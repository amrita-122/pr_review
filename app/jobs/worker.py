"""Job worker: `python -m app.jobs.worker` (a separate process from the web server).

Lifecycle of a job: queued -> running -> done | failed (or back to queued for a retry).
- Claiming uses SELECT ... FOR UPDATE SKIP LOCKED, so any number of workers can run side by side
  and no job is taken twice. The claim is committed immediately: no database transaction is held
  open while a review (a minute of model calls) runs.
- A job whose worker died stays `running` until its lease expires, then goes back to the queue.
- On SIGTERM the worker stops taking jobs, lets the current one finish within a grace period, and
  otherwise puts it back in the queue without using up an attempt.
"""

import asyncio
import contextlib
import os
import random
import signal
import socket
import sys
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import structlog
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.db import runs
from app.db.models import Job
from app.db.session import get_session
from app.jobs.errors import INTERNAL, JobFailure, classify
from app.jobs.handlers import (
    expire_pending_reviews,
    post_failure_notice,
    run_confirm_job,
    run_review_job,
)
from app.jobs.payload import CONFIRM_REVIEW, REVIEW_PR, ConfirmJobPayload, ReviewJobPayload
from app.logging import configure_logging
from app.observability import configure_tracing
from app.review.checkpoint import setup_checkpointer

logger = structlog.get_logger()

MAX_BACKOFF_SECONDS = 600.0
EXPIRY_CHECK_SECONDS = 60.0


@dataclass(frozen=True)
class ClaimedJob:
    """A plain copy of the job row, safe to use after the claiming session is closed."""

    id: int
    kind: str
    payload: dict[str, Any]
    attempts: int  # including this one
    max_attempts: int


def utcnow() -> datetime:
    return datetime.now(UTC)


def backoff_delay(attempts: int, base: float) -> float:
    """Seconds to wait before the next try: base, 2x, 4x ... capped, with +-20% jitter."""
    raw = min(base * 2 ** max(attempts - 1, 0), MAX_BACKOFF_SECONDS)
    return float(raw * random.uniform(0.8, 1.2))  # noqa: S311  (jitter, not security)


# --- queue operations (each takes a session and commits) -------------------------------------


def claim_job(session: Session, worker_id: str, now: datetime) -> ClaimedJob | None:
    job = session.scalars(
        select(Job)
        .where(Job.status == "queued", Job.run_after <= now)
        .order_by(Job.run_after, Job.id)
        .limit(1)
        .with_for_update(skip_locked=True)
    ).first()
    if job is None:
        return None
    job.status = "running"
    job.locked_at = now
    job.locked_by = worker_id
    job.attempts += 1
    session.commit()  # release the row lock right away; the lease protects the job from here on
    return ClaimedJob(job.id, job.kind, dict(job.payload), job.attempts, job.max_attempts)


def requeue_stale(
    session: Session, now: datetime, lease_seconds: float
) -> list[tuple[str, dict[str, Any]]]:
    """Jobs whose worker vanished: back to the queue, or failed if they are out of attempts.

    Returns (kind, payload) of jobs that just failed for good, so the caller can tell the PR.
    A /confirm job that lost its worker puts its run back to awaiting_confirmation, so the retry
    (or the maintainer) can claim it again instead of finding it stuck in `confirming`.
    """
    cutoff = now - timedelta(seconds=lease_seconds)
    stale = session.scalars(
        select(Job)
        .where(Job.status == "running", Job.locked_at < cutoff)
        .with_for_update(skip_locked=True)
    ).all()
    gave_up: list[tuple[str, dict[str, Any]]] = []
    for job in stale:
        job.locked_at = None
        job.locked_by = None
        job.last_error = "lease expired (worker stopped)"
        if job.attempts >= job.max_attempts:
            job.status = "failed"
            gave_up.append((job.kind, dict(job.payload)))
        else:
            job.status = "queued"
            job.run_after = now
    session.commit()
    for job in stale:
        if job.kind == CONFIRM_REVIEW and isinstance(job.payload.get("run_id"), str):
            runs.revert_to_awaiting(session, job.payload["run_id"])
    if stale:
        logger.warning("stale_jobs_recovered", count=len(stale), failed=len(gave_up))
    return gave_up


def complete_job(session: Session, job_id: int) -> None:
    job = session.get(Job, job_id)
    if job is not None:
        job.status = "done"
        job.locked_at = None
        job.locked_by = None
        job.last_error = None
        session.commit()


def schedule_retry(
    session: Session, job_id: int, *, delay_seconds: float, error: str, now: datetime
) -> None:
    job = session.get(Job, job_id)
    if job is not None:
        job.status = "queued"
        job.run_after = now + timedelta(seconds=delay_seconds)
        job.locked_at = None
        job.locked_by = None
        job.last_error = error[:500]
        session.commit()


def fail_job(session: Session, job_id: int, *, error: str) -> None:
    job = session.get(Job, job_id)
    if job is not None:
        job.status = "failed"
        job.locked_at = None
        job.locked_by = None
        job.last_error = error[:500]
        session.commit()


def release_job(session: Session, job_id: int, now: datetime) -> None:
    """Shutdown mid-job: back to the queue, attempt given back (not the job's fault)."""
    job = session.get(Job, job_id)
    if job is not None:
        job.status = "queued"
        job.attempts = max(job.attempts - 1, 0)
        job.run_after = now
        job.locked_at = None
        job.locked_by = None
        job.last_error = "released on shutdown"
        session.commit()


# --- running a claimed job ------------------------------------------------------------------


def _parse(claimed: ClaimedJob) -> ReviewJobPayload | ConfirmJobPayload:
    if claimed.kind not in (REVIEW_PR, CONFIRM_REVIEW):
        raise JobFailure(INTERNAL, False, f"unknown job kind {claimed.kind!r}"[:100])
    try:
        if claimed.kind == CONFIRM_REVIEW:
            return ConfirmJobPayload.model_validate(claimed.payload)
        return ReviewJobPayload.model_validate(claimed.payload)
    except ValidationError:
        raise JobFailure(INTERNAL, False, "invalid job payload") from None


async def _execute(claimed: ClaimedJob, settings: Settings) -> str:
    payload = _parse(claimed)
    async with asyncio.timeout(settings.job_deadline_seconds):
        if isinstance(payload, ConfirmJobPayload):
            return await run_confirm_job(payload)
        return await run_review_job(payload)


def _revert_confirm(claimed: ClaimedJob) -> None:
    """A /confirm attempt did not finish: the review is still held, a maintainer can try again."""
    run_id = claimed.payload.get("run_id")
    if claimed.kind == CONFIRM_REVIEW and isinstance(run_id, str):
        with get_session() as session:
            runs.revert_to_awaiting(session, run_id)


async def _on_failure(claimed: ClaimedJob, failure: JobFailure, settings: Settings) -> str:
    final = (not failure.transient) or claimed.attempts >= claimed.max_attempts
    run_id = claimed.payload.get("run_id")
    now = utcnow()
    if claimed.kind == CONFIRM_REVIEW:
        # A failed /confirm never fails the run and never posts "review failed": the review is
        # still held and the maintainer can reply /confirm again.
        _revert_confirm(claimed)
        if final:
            with get_session() as session:
                fail_job(session, claimed.id, error=failure.message)
            logger.warning("confirm_job_failed", job_id=claimed.id, reason=failure.reason)
            return "failed"
        delay = backoff_delay(claimed.attempts, settings.job_backoff_base_seconds)
        with get_session() as session:
            schedule_retry(session, claimed.id, delay_seconds=delay, error=failure.message, now=now)
        logger.warning("confirm_job_will_retry", job_id=claimed.id, reason=failure.reason)
        return "retry"
    if final:
        with get_session() as session:
            fail_job(session, claimed.id, error=failure.message)
        if isinstance(run_id, str):
            with get_session() as session:
                runs.finish_run(
                    session,
                    run_id,
                    status=runs.FAILED,
                    error=failure.message,
                    failure_reason=failure.reason,
                )
        with contextlib.suppress(ValidationError):
            await post_failure_notice(ReviewJobPayload.model_validate(claimed.payload))
        logger.warning(
            "job_failed", job_id=claimed.id, reason=failure.reason, attempts=claimed.attempts
        )
        return "failed"
    delay = backoff_delay(claimed.attempts, settings.job_backoff_base_seconds)
    with get_session() as session:
        schedule_retry(session, claimed.id, delay_seconds=delay, error=failure.message, now=now)
    if isinstance(run_id, str):
        with get_session() as session:
            runs.set_retrying(session, run_id, error=failure.message, reason=failure.reason)
    logger.warning(
        "job_will_retry",
        job_id=claimed.id,
        reason=failure.reason,
        attempts=claimed.attempts,
        retry_in_seconds=round(delay),
    )
    return "retry"


async def run_claimed(claimed: ClaimedJob, settings: Settings, stop: asyncio.Event) -> str:
    """Run one claimed job to a resting state: done, retry, failed or released (shutdown)."""
    task = asyncio.create_task(_execute(claimed, settings))
    stop_task = asyncio.create_task(stop.wait())
    try:
        await asyncio.wait({task, stop_task}, return_when=asyncio.FIRST_COMPLETED)
        if not task.done():  # shutdown requested while the job is running
            await asyncio.wait({task}, timeout=settings.shutdown_grace_seconds)
            if not task.done():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task
                with get_session() as session:
                    release_job(session, claimed.id, utcnow())
                _revert_confirm(claimed)
                logger.warning("job_released_on_shutdown", job_id=claimed.id)
                return "released"
    finally:
        stop_task.cancel()
    try:
        task.result()
    except Exception as exc:  # classified: transient -> retry, permanent -> failed
        return await _on_failure(claimed, classify(exc), settings)
    with get_session() as session:
        complete_job(session, claimed.id)
    return "done"


async def _notify_gave_up(jobs: list[tuple[str, dict[str, Any]]]) -> None:
    for kind, raw in jobs:
        if kind == CONFIRM_REVIEW:
            continue  # requeue_stale already put the run back to awaiting; nothing to announce
        run_id = raw.get("run_id")
        if isinstance(run_id, str):
            with get_session() as session:
                runs.finish_run(
                    session,
                    run_id,
                    status=runs.FAILED,
                    error="lease expired (worker stopped)",
                    failure_reason="internal",
                )
        with contextlib.suppress(ValidationError):
            await post_failure_notice(ReviewJobPayload.model_validate(raw))


async def run_worker(settings: Settings, stop: asyncio.Event, *, once: bool = False) -> None:
    """The main loop. With once=True it returns as soon as the queue is empty (used by tests)."""
    worker_id = f"{socket.gethostname()}-{os.getpid()}-{uuid.uuid4().hex[:6]}"
    logger.info("worker_started", worker_id=worker_id)
    next_expiry_check = 0.0
    while not stop.is_set():
        if time.monotonic() >= next_expiry_check:  # held reviews: once a minute is plenty
            next_expiry_check = time.monotonic() + EXPIRY_CHECK_SECONDS
            try:
                await expire_pending_reviews(settings)
            except Exception as exc:  # housekeeping must never stop the worker
                logger.warning("expiry_check_failed", error=type(exc).__name__)
        try:
            with get_session() as session:
                gave_up = requeue_stale(session, utcnow(), settings.job_lease_seconds)
            await _notify_gave_up(gave_up)
            with get_session() as session:
                claimed = claim_job(session, worker_id, utcnow())
        except Exception as exc:  # e.g. the database is briefly unreachable: keep the loop alive
            logger.warning("worker_poll_failed", error=type(exc).__name__)
            claimed = None
            if once:
                raise
        if claimed is None:
            if once:
                return
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=settings.job_poll_seconds)
            continue
        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(job_id=claimed.id)  # on every line of this job
        logger.info("job_started", kind=claimed.kind, attempt=claimed.attempts)
        outcome = await run_claimed(claimed, settings, stop)
        logger.info("job_settled", outcome=outcome)
        structlog.contextvars.clear_contextvars()  # idle-loop lines must not carry the last job id
    logger.info("worker_stopped", worker_id=worker_id)


def _install_signal_handlers(loop: asyncio.AbstractEventLoop, stop: asyncio.Event) -> None:
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:  # Windows event loops: fall back to the plain signal module
            signal.signal(sig, lambda *_: loop.call_soon_threadsafe(stop.set))


async def amain() -> None:
    settings = get_settings()
    configure_logging()
    configure_tracing(settings)
    if settings.confirmation_enabled:
        await setup_checkpointer(settings)  # the checkpointer's own tables; idempotent
    stop = asyncio.Event()
    _install_signal_handlers(asyncio.get_running_loop(), stop)
    await run_worker(settings, stop)


if __name__ == "__main__":
    if sys.platform == "win32":
        # psycopg's async mode (the checkpointer) cannot use Windows' default Proactor loop.
        asyncio.run(amain(), loop_factory=asyncio.SelectorEventLoop)
    else:
        asyncio.run(amain())
