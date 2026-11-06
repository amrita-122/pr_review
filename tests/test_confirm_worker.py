"""Worker side of /confirm: dispatch by job kind, and what a failure does to the held run."""

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta

import httpx
import pytest
import respx
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import runs
from app.db.models import Job, ReviewRun
from app.jobs import worker
from app.jobs.payload import CONFIRM_REVIEW, ConfirmJobPayload

Drain = Callable[[], Awaitable[None]]
RUN_ID = "run1"


def hold_run(db: Session, *, status: str = runs.AWAITING_CONFIRMATION) -> None:
    runs.create_queued(
        db, run_id=RUN_ID, repo="o/r", pr_number=7, head_sha="a" * 40, prompt_version="v", model="m"
    )
    assert runs.mark_running(db, RUN_ID, prompt_version="v", model="m")
    assert runs.mark_awaiting(db, RUN_ID, installation_id=1)
    if status == runs.CONFIRMING:
        assert runs.claim_confirmation(db, RUN_ID, login="maint", now=datetime.now(UTC))


def add_confirm_job(db: Session, **job_fields: object) -> None:
    payload = ConfirmJobPayload(
        run_id=RUN_ID,
        installation_id=1,
        owner="o",
        repo="r",
        number=7,
        comment_id=99,
        commenter_login="maint",
    )
    db.add(Job(kind=CONFIRM_REVIEW, payload=payload.model_dump(), **job_fields))
    db.commit()


def state(db: Session) -> tuple[ReviewRun, Job]:
    db.expire_all()
    return db.get(ReviewRun, RUN_ID), db.scalars(select(Job)).one()  # type: ignore[return-value]


@respx.mock
async def test_confirm_job_is_dispatched_to_its_handler(
    db_session: Session, drain: Drain, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[ConfirmJobPayload] = []

    async def handler(payload: ConfirmJobPayload) -> str:
        seen.append(payload)
        return "posted"

    monkeypatch.setattr(worker, "run_confirm_job", handler)
    hold_run(db_session)
    add_confirm_job(db_session)
    await drain()
    assert [p.commenter_login for p in seen] == ["maint"]
    assert state(db_session)[1].status == "done"


@respx.mock
async def test_failed_confirm_reverts_to_awaiting_and_retries_without_any_comment(
    db_session: Session, drain: Drain, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def boom(payload: ConfirmJobPayload) -> str:
        # what a real handler leaves behind: the run claimed (confirming), then GitHub fails
        assert runs.claim_confirmation(db_session, RUN_ID, login="maint", now=datetime.now(UTC))
        raise httpx.ConnectError("down")

    comments = respx.post(url__regex=r".*/comments").mock(return_value=httpx.Response(201))
    monkeypatch.setattr(worker, "run_confirm_job", boom)
    hold_run(db_session)
    add_confirm_job(db_session)
    await drain()
    run, job = state(db_session)
    assert run.status == runs.AWAITING_CONFIRMATION  # not failed: the maintainer can still confirm
    assert (run.confirmed_by, run.failure_reason) == (None, None)
    assert job.status == "queued" and job.attempts == 1  # will retry later
    assert comments.call_count == 0  # no "review failed" comment


@respx.mock
async def test_confirm_job_out_of_attempts_fails_the_job_but_not_the_run(
    db_session: Session, drain: Drain, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def boom(payload: ConfirmJobPayload) -> str:
        raise httpx.ConnectError("down")

    comments = respx.post(url__regex=r".*/comments").mock(return_value=httpx.Response(201))
    monkeypatch.setattr(worker, "run_confirm_job", boom)
    hold_run(db_session)
    add_confirm_job(db_session, max_attempts=1)
    await drain()
    run, job = state(db_session)
    assert (run.status, job.status) == (runs.AWAITING_CONFIRMATION, "failed")
    assert comments.call_count == 0


def test_stale_confirm_job_puts_the_run_back_to_awaiting(db_session: Session) -> None:
    now = datetime.now(UTC)
    hold_run(db_session, status=runs.CONFIRMING)
    add_confirm_job(db_session)
    db_session.execute(
        update(Job).values(status="running", locked_at=now - timedelta(hours=1), attempts=1)
    )
    db_session.commit()
    gave_up = worker.requeue_stale(db_session, now, get_settings().job_lease_seconds)
    run, job = state(db_session)
    assert gave_up == [] and job.status == "queued"
    assert run.status == runs.AWAITING_CONFIRMATION  # the retry can claim it again


async def test_stale_confirm_job_out_of_attempts_is_not_reported_as_a_failed_review(
    db_session: Session,
) -> None:
    now = datetime.now(UTC)
    hold_run(db_session, status=runs.CONFIRMING)
    add_confirm_job(db_session)
    db_session.execute(
        update(Job).values(status="running", locked_at=now - timedelta(hours=1), attempts=3)
    )
    db_session.commit()
    gave_up = worker.requeue_stale(db_session, now, 600)
    await worker._notify_gave_up(gave_up)
    run, job = state(db_session)
    assert job.status == "failed"
    assert run.status == runs.AWAITING_CONFIRMATION and run.failure_reason is None
