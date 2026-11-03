"""Persistence for review runs and findings.

State transitions here are what make retries safe: a superseded run is never revived by a retry,
posting is recorded the moment it succeeds, and the failure comment can be claimed only once.
Callers decide which writes are critical (let them raise) and which are bookkeeping (swallow).
"""

from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import delete, select, update
from sqlalchemy.orm import Session

from app.db.models import FindingRow, ReviewRun
from app.review.schemas import Finding

MAX_ERROR_CHARS = 500

QUEUED = "queued"
RUNNING = "running"
OK = "ok"
FAILED = "failed"
SUPERSEDED = "superseded"
IN_FLIGHT = (QUEUED, RUNNING)


def get_run(session: Session, run_id: str) -> ReviewRun | None:
    return session.get(ReviewRun, run_id)


def get_by_key(session: Session, repo: str, pr_number: int, head_sha: str) -> ReviewRun | None:
    return session.scalars(
        select(ReviewRun).where(
            ReviewRun.repo == repo,
            ReviewRun.pr_number == pr_number,
            ReviewRun.head_sha == head_sha,
        )
    ).first()


def create_queued(
    session: Session,
    *,
    run_id: str,
    repo: str,
    pr_number: int,
    head_sha: str,
    prompt_version: str,
    model: str,
) -> None:
    """Add (not commit) a queued run: the caller commits it together with the job."""
    session.add(
        ReviewRun(
            id=run_id,
            repo=repo,
            pr_number=pr_number,
            head_sha=head_sha,
            status=QUEUED,
            prompt_version=prompt_version,
            model=model,
        )
    )


def supersede_older(session: Session, repo: str, pr_number: int, keep_sha: str) -> int:
    """A newer push replaces every in-flight run for other commits of the same PR (no commit)."""
    result = session.execute(
        update(ReviewRun)
        .where(
            ReviewRun.repo == repo,
            ReviewRun.pr_number == pr_number,
            ReviewRun.head_sha != keep_sha,
            ReviewRun.status.in_(IN_FLIGHT),
        )
        .values(status=SUPERSEDED, finished_at=datetime.now(UTC))
    )
    return int(result.rowcount)  # type: ignore[attr-defined]


def mark_running(session: Session, run_id: str, *, prompt_version: str, model: str) -> bool:
    """queued/failed/running -> running. False if the run is gone or was superseded."""
    run = session.get(ReviewRun, run_id)
    if run is None or run.status == SUPERSEDED:
        return False
    run.status = RUNNING
    run.prompt_version = prompt_version
    run.model = model
    session.commit()
    return True


def mark_superseded(session: Session, run_id: str) -> None:
    run = session.get(ReviewRun, run_id)
    if run is not None and run.status != SUPERSEDED:
        run.status = SUPERSEDED
        run.finished_at = datetime.now(UTC)
        session.commit()


def is_superseded(session: Session, run_id: str) -> bool:
    run = session.get(ReviewRun, run_id)
    return run is not None and run.status == SUPERSEDED


def set_retrying(session: Session, run_id: str, *, error: str, reason: str) -> None:
    """An attempt failed but the job will be retried: back to queued, remember why."""
    run = session.get(ReviewRun, run_id)
    if run is None or run.status == SUPERSEDED:
        return
    run.status = QUEUED
    run.error = error[:MAX_ERROR_CHARS]
    run.failure_reason = reason
    session.commit()


def mark_posted(session: Session, run_id: str) -> None:
    run = session.get(ReviewRun, run_id)
    if run is not None:
        run.posted_at = datetime.now(UTC)
        session.commit()


def add_usage(
    session: Session, run_id: str, *, tokens_in: int, tokens_out: int, cost_usd: Decimal
) -> None:
    """Accumulate across attempts: a retried run really did spend both attempts' tokens."""
    run = session.get(ReviewRun, run_id)
    if run is None:
        return
    run.tokens_in += tokens_in
    run.tokens_out += tokens_out
    run.cost_usd += cost_usd
    session.commit()


def finish_run(
    session: Session,
    run_id: str,
    *,
    status: str,
    error: str | None = None,
    failure_reason: str | None = None,
) -> None:
    run = session.get(ReviewRun, run_id)
    if run is None or run.status == SUPERSEDED:
        return  # a superseded run stays superseded
    run.status = status
    run.finished_at = datetime.now(UTC)
    # Exception type + short message only: never a stack trace (may hold URLs or secrets).
    run.error = error[:MAX_ERROR_CHARS] if error else None
    run.failure_reason = failure_reason
    session.commit()


def claim_failure_comment(session: Session, run_id: str) -> bool:
    """True exactly once per run, and never for a superseded run: the right to post the notice."""
    result = session.execute(
        update(ReviewRun)
        .where(
            ReviewRun.id == run_id,
            ReviewRun.failure_comment_posted.is_(False),
            ReviewRun.status != SUPERSEDED,
        )
        .values(failure_comment_posted=True)
    )
    session.commit()
    return bool(result.rowcount)  # type: ignore[attr-defined]


def save_findings(
    session: Session,
    run_id: str,
    posted: list[Finding],
    dropped: list[tuple[Finding, str]],
) -> None:
    session.execute(delete(FindingRow).where(FindingRow.run_id == run_id))  # a retry replaces
    rows = [(f, True, None) for f in posted] + [(f, False, reason) for f, reason in dropped]
    session.add_all(
        FindingRow(
            run_id=run_id,
            file=f.file_path,
            line=f.line,
            severity=f.severity.value,
            category=f.category.value,
            title=f.title,
            posted=is_posted,
            drop_reason=reason,
        )
        for f, is_posted, reason in rows
    )
    session.commit()
