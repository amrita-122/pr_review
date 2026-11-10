"""The review job with human confirmation on: hold critical findings, post the rest as before."""

import json
from collections.abc import Awaitable, Callable

import httpx
import pytest
import respx
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import runs
from app.db.models import FindingRow, Job
from app.jobs import handlers
from app.jobs.handlers import pending_comment, run_review_job
from app.jobs.payload import CONFIRM_REVIEW, ConfirmJobPayload, ReviewJobPayload
from app.review.schemas import Category, Finding, Severity
from tests.helpers import Pipeline
from tests.test_confirm_job import Saver
from tests.test_jobs import Drain, Enqueue, make_event, run_of

EVENT = make_event()
Reviewer = Callable[[str, str], Awaitable[list[Finding]]]


def reviewer_of(severity: Severity) -> Reviewer:
    async def reviewer(path: str, numbered: str) -> list[Finding]:
        return [
            Finding(
                file_path=path,
                line=2,
                severity=severity,
                category=Category.INJECTION,
                title="finding",
                rationale="r",
            )
        ]

    return reviewer


@pytest.fixture
def held(monkeypatch: pytest.MonkeyPatch) -> Saver:
    """Confirmation on, a shared in-memory checkpointer, and a reviewer that finds a CRITICAL."""
    shared = Saver()
    monkeypatch.setattr(handlers, "open_checkpointer", shared)
    monkeypatch.setenv("CONFIRMATION_ENABLED", "true")
    get_settings.cache_clear()
    monkeypatch.setattr(handlers, "build_reviewers", lambda: reviewer_of(Severity.CRITICAL))
    return shared


def thread_exists(saver: Saver, run_id: str) -> bool:
    return saver.saver.get_tuple({"configurable": {"thread_id": run_id}}) is not None


def comment_bodies(gh: Pipeline) -> list[str]:
    return [json.loads(call.request.content)["body"] for call in gh.comments.calls]


def test_pending_comment_is_static_text() -> None:
    text = pending_comment(2, frozenset({Severity.CRITICAL}))
    assert text.startswith("2 critical finding(s) pending") and "`/confirm`" in text
    assert pending_comment(0, frozenset({Severity.CRITICAL})).startswith("Critical finding(s)")


@respx.mock
async def test_critical_finding_holds_the_review(
    db_session: Session, held: Saver, enqueue: Enqueue, drain: Drain
) -> None:
    gh = Pipeline(EVENT)
    enqueue(EVENT)
    await drain()

    run = run_of(db_session)
    assert gh.reviews.call_count == 0  # nothing about the vulnerability is public
    assert run.status == runs.AWAITING_CONFIRMATION and run.awaiting_since is not None
    assert run.installation_id == EVENT.installation_id and run.posted_at is None
    assert thread_exists(held, run.id)
    assert db_session.scalar(select(func.count()).select_from(FindingRow)) == 0
    bodies = comment_bodies(gh)
    assert len(bodies) == 1 and "/confirm" in bodies[0]
    assert db_session.scalars(select(Job)).one().status == "done"


@respx.mock
async def test_a_retried_job_does_not_review_or_announce_again(
    db_session: Session, held: Saver, enqueue: Enqueue, drain: Drain
) -> None:
    gh = Pipeline(EVENT)
    enqueue(EVENT)
    await drain()
    payload = ReviewJobPayload.from_event(EVENT, run_of(db_session).id)

    assert await run_review_job(payload) == "pending"  # e.g. the worker died after the hold

    assert len(comment_bodies(gh)) == 1
    assert gh.reviews.call_count == 0
    assert run_of(db_session).status == runs.AWAITING_CONFIRMATION


@respx.mock
async def test_a_failing_pending_comment_does_not_fail_the_job_and_is_sent_later(
    db_session: Session, held: Saver, enqueue: Enqueue, drain: Drain
) -> None:
    """10.7: a failed POST must not leave the maintainers uninformed."""
    gh = Pipeline(EVENT)
    gh.comments.mock(return_value=httpx.Response(500))
    enqueue(EVENT)
    await drain()
    run = run_of(db_session)
    assert run.status == runs.AWAITING_CONFIRMATION
    assert db_session.scalars(select(Job)).one().status == "done"  # the paid review is not redone
    assert run.pending_comment_posted is False  # the claim was given back
    assert run.held_count == 1

    gh.comments.mock(return_value=httpx.Response(201, json={}))
    assert await handlers.announce_unannounced_holds(get_settings()) == 1
    assert await handlers.announce_unannounced_holds(get_settings()) == 0  # once, not forever
    assert "1 critical finding(s) pending" in comment_bodies(gh)[-1]
    assert run_of(db_session).pending_comment_posted is True


@respx.mock
async def test_non_critical_review_is_posted_as_before(
    db_session: Session,
    held: Saver,
    enqueue: Enqueue,
    drain: Drain,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(handlers, "build_reviewers", lambda: reviewer_of(Severity.HIGH))
    gh = Pipeline(EVENT)
    enqueue(EVENT)
    await drain()

    assert gh.reviews.call_count == 1 and gh.comments.call_count == 0
    body = gh.sent_review()["body"]
    assert body.count("uv.lock") == 1 and body.count("logo.png") == 1  # skips listed once
    assert run_of(db_session).status == runs.OK


@respx.mock
async def test_flag_off_posts_critical_findings_as_before(
    db_session: Session,
    held: Saver,
    enqueue: Enqueue,
    drain: Drain,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CONFIRMATION_ENABLED", "false")
    get_settings.cache_clear()
    gh = Pipeline(EVENT)
    enqueue(EVENT)
    await drain()
    assert gh.reviews.call_count == 1 and gh.comments.call_count == 0
    assert run_of(db_session).status == runs.OK


@respx.mock
async def test_a_newer_push_removes_the_old_held_checkpoint(
    db_session: Session, held: Saver, enqueue: Enqueue, drain: Drain
) -> None:
    old, new = (
        make_event("a" * 40, "2026-11-10T10:00:00Z"),
        make_event("b" * 40, "2026-11-10T10:00:05Z"),
    )
    gh = Pipeline(old)
    enqueue(old)
    await drain()
    old_id = run_of(db_session, "a" * 40).id
    assert thread_exists(held, old_id)

    gh.head.mock(return_value=httpx.Response(200, json={"head": {"sha": "b" * 40}}))
    enqueue(new)  # the push supersedes the held run
    await drain()

    assert run_of(db_session, "a" * 40).status == runs.SUPERSEDED
    assert not thread_exists(held, old_id)  # source code does not linger in the database


@respx.mock
async def test_hold_then_confirm_posts_the_review_exactly_once(
    db_session: Session, held: Saver, enqueue: Enqueue, drain: Drain
) -> None:
    gh = Pipeline(EVENT)
    perm = (
        f"https://api.github.com/repos/{EVENT.owner}/{EVENT.repo}"
        "/collaborators/maintainer/permission"
    )
    respx.get(perm).mock(return_value=httpx.Response(200, json={"permission": "admin"}))
    enqueue(EVENT)
    await drain()
    run = run_of(db_session)
    assert gh.reviews.call_count == 0

    confirm = ConfirmJobPayload(
        run_id=run.id,
        installation_id=EVENT.installation_id,
        owner=EVENT.owner,
        repo=EVENT.repo,
        number=EVENT.number,
        comment_id=9,
        commenter_login="maintainer",
    )
    db_session.add(Job(kind=CONFIRM_REVIEW, payload=confirm.model_dump()))
    db_session.commit()
    await drain()

    assert gh.reviews.call_count == 1
    sent = gh.sent_review()
    assert [(c["path"], c["line"]) for c in sent["comments"]] == [("app/db.py", 2)]
    assert sent["body"].count("uv.lock") == 1
    assert run_of(db_session).status == runs.OK
    assert not thread_exists(held, run.id)


@respx.mock
async def test_only_the_critical_finding_is_held_the_rest_of_the_review_posts_now(
    db_session: Session,
    held: Saver,
    enqueue: Enqueue,
    drain: Drain,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """10.8 (ADR 0007 amended): a medium finding is not withheld because a critical one is held.
    The critical one follows as a second review after /confirm; nothing is posted twice."""
    from tests.helpers import FILES

    async def mixed(path: str, numbered: str) -> list[Finding]:
        severity = Severity.CRITICAL if path == "app/db.py" else Severity.MEDIUM
        return await reviewer_of(severity)(path, numbered)

    monkeypatch.setattr(handlers, "build_reviewers", lambda: mixed)
    gh = Pipeline(EVENT)
    other = {"filename": "app/other.py", "status": "modified", "patch": FILES[0]["patch"]}
    gh.files.mock(return_value=httpx.Response(200, json=[*FILES, other]))
    perm = (
        f"https://api.github.com/repos/{EVENT.owner}/{EVENT.repo}"
        "/collaborators/maintainer/permission"
    )
    respx.get(perm).mock(return_value=httpx.Response(200, json={"permission": "admin"}))

    enqueue(EVENT)
    await drain()

    # 1. Immediately: the medium finding only, and a note that more is pending (no severity named).
    assert gh.reviews.call_count == 1
    first = gh.sent_review(0)
    assert [c["path"] for c in first["comments"]] == ["app/other.py"]
    assert "1 more finding(s) are pending" in first["body"] and "critical" not in first["body"]
    run = run_of(db_session)
    assert run.status == runs.AWAITING_CONFIRMATION and run.posted_at is not None
    assert len(comment_bodies(gh)) == 1 and "/confirm" in comment_bodies(gh)[0]
    # a worker retry of the review job does not post the first review again
    payload = ReviewJobPayload.from_event(EVENT, run.id)
    assert await run_review_job(payload) == "pending"
    assert gh.reviews.call_count == 1

    # 2. After /confirm: a follow-up review with ONLY the critical finding, under its own marker.
    confirm = ConfirmJobPayload(
        run_id=run.id,
        installation_id=EVENT.installation_id,
        owner=EVENT.owner,
        repo=EVENT.repo,
        number=EVENT.number,
        comment_id=9,
        commenter_login="maintainer",
    )
    db_session.add(Job(kind=CONFIRM_REVIEW, payload=confirm.model_dump()))
    db_session.commit()
    await drain()

    assert gh.reviews.call_count == 2
    second = gh.sent_review(1)
    assert [c["path"] for c in second["comments"]] == ["app/db.py"]
    assert handlers.review_marker(run.id, held=True) in second["body"]
    assert handlers.review_marker(run.id, held=True) != handlers.review_marker(run.id)
    final = run_of(db_session)
    assert final.status == runs.OK and final.held_posted_at is not None
    assert not thread_exists(held, run.id)
