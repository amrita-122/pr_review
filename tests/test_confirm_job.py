"""run_confirm_job: who may confirm, and that a confirmed review is posted exactly once."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import httpx
import pytest
import respx
from langgraph.checkpoint.memory import InMemorySaver
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import runs
from app.db.models import FindingRow, Job, ReviewRun
from app.diff.parse import parse_patch
from app.jobs import handlers
from app.jobs.handlers import is_allowed, review_marker, run_confirm_job
from app.jobs.payload import CONFIRM_REVIEW, ConfirmJobPayload
from app.review.checkpoint import checkpoint_serde
from app.review.graph import ReviewLimits, review_diff
from app.review.schemas import Category, Finding, Severity
from tests.helpers import FILES, Pipeline
from tests.test_jobs import Drain, make_event

PATH = FILES[0]["filename"]
EVENT = make_event()
RUN_ID = "run1"
REPO = f"{EVENT.owner}/{EVENT.repo}"
PERMISSION_URL = (
    f"https://api.github.com/repos/{EVENT.owner}/{EVENT.repo}/collaborators/maintainer/permission"
)


class Saver:
    """One in-memory checkpointer shared by the review and the confirm job, like Postgres."""

    def __init__(self) -> None:
        self.saver = InMemorySaver(serde=checkpoint_serde())

    @asynccontextmanager
    async def __call__(self, _settings: Any) -> AsyncIterator[InMemorySaver]:
        yield self.saver

    def has_thread(self) -> bool:
        return self.saver.get_tuple({"configurable": {"thread_id": RUN_ID}}) is not None


@pytest.fixture
def saver(monkeypatch: pytest.MonkeyPatch) -> Saver:
    shared = Saver()
    monkeypatch.setattr(handlers, "open_checkpointer", shared)
    monkeypatch.setenv("CONFIRMATION_ENABLED", "true")
    get_settings.cache_clear()
    return shared


async def critical_reviewer(path: str, numbered: str) -> list[Finding]:
    return [
        Finding(
            file_path=path,
            line=2,
            severity=Severity.CRITICAL,
            category=Category.INJECTION,
            title="SQL injection",
            rationale="r",
        )
    ]


async def hold_review(db: Session, saver: Saver) -> None:
    """What the review job leaves behind: a run waiting for /confirm and its paused graph."""
    runs.create_queued(
        db,
        run_id=RUN_ID,
        repo=REPO,
        pr_number=EVENT.number,
        head_sha=EVENT.head_sha,
        prompt_version="v",
        model="m",
    )
    assert runs.mark_running(db, RUN_ID, prompt_version="v", model="m")
    outcome = await review_diff(
        {PATH: parse_patch(PATH, FILES[0]["patch"])},
        critical_reviewer,
        ReviewLimits(max_files=10, max_chars_per_file=10_000),
        hold_severities=frozenset({Severity.CRITICAL}),
        checkpointer=saver.saver,
        thread_id=RUN_ID,
    )
    assert outcome.pending == 1
    assert runs.mark_awaiting(db, RUN_ID, installation_id=EVENT.installation_id)


def confirm(login: str = "maintainer") -> ConfirmJobPayload:
    return ConfirmJobPayload(
        run_id=RUN_ID,
        installation_id=EVENT.installation_id,
        owner=EVENT.owner,
        repo=EVENT.repo,
        number=EVENT.number,
        comment_id=5,
        commenter_login=login,
    )


def permission(level: str | None) -> respx.Route:
    if level is None:
        response = httpx.Response(404)
    else:
        response = httpx.Response(200, json={"permission": level})
    return respx.get(PERMISSION_URL).mock(return_value=response)


def run_row(db: Session) -> ReviewRun:
    db.expire_all()
    return db.get(ReviewRun, RUN_ID)  # type: ignore[return-value]


# --- who may confirm ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("level", "allowed"),
    [
        ("admin", True),
        ("write", True),
        ("read", False),
        ("none", False),
        ("triage", False),
        ("maintain", False),
        ("", False),
        (None, False),
    ],
)
def test_only_admin_and_write_are_allowed(level: str | None, allowed: bool) -> None:
    assert is_allowed(level) is allowed


@respx.mock
@pytest.mark.parametrize("level", ["write", "admin"])
async def test_maintainer_confirm_posts_the_held_review_once(
    db_session: Session, saver: Saver, level: str
) -> None:
    gh = Pipeline(EVENT)
    permission(level)
    await hold_review(db_session, saver)

    assert await run_confirm_job(confirm()) == "posted"

    sent = gh.sent_review()
    assert gh.reviews.call_count == 1
    assert (sent["event"], sent["commit_id"]) == ("COMMENT", EVENT.head_sha)
    assert [(c["path"], c["line"]) for c in sent["comments"]] == [(PATH, 2)]
    assert review_marker(RUN_ID) in sent["body"]
    run = run_row(db_session)
    assert (run.status, run.confirmed_by) == (runs.OK, "maintainer")
    assert run.posted_at is not None
    assert not saver.has_thread()  # the checkpoint holds source code: gone
    rows = db_session.scalars(select(FindingRow)).all()
    assert [(r.severity, r.posted) for r in rows] == [("critical", True)]


@respx.mock
@pytest.mark.parametrize("level", ["read", "none", "triage", None])
async def test_anyone_else_changes_nothing(
    db_session: Session, saver: Saver, level: str | None
) -> None:
    gh = Pipeline(EVENT)
    permission(level)
    await hold_review(db_session, saver)

    assert await run_confirm_job(confirm()) == "ignored"

    assert gh.reviews.call_count == 0
    run = run_row(db_session)
    assert (run.status, run.confirmed_by) == (runs.AWAITING_CONFIRMATION, None)
    assert saver.has_thread()  # still held: a real maintainer can still confirm


@respx.mock
async def test_github_error_on_the_permission_check_raises_and_posts_nothing(
    db_session: Session, saver: Saver
) -> None:
    gh = Pipeline(EVENT)
    respx.get(PERMISSION_URL).mock(return_value=httpx.Response(500))
    await hold_review(db_session, saver)

    with pytest.raises(httpx.HTTPStatusError):
        await run_confirm_job(confirm())

    assert gh.reviews.call_count == 0
    assert run_row(db_session).status == runs.AWAITING_CONFIRMATION


# --- exactly once ------------------------------------------------------------------------------


@respx.mock
async def test_a_second_confirm_posts_nothing(db_session: Session, saver: Saver) -> None:
    gh = Pipeline(EVENT)
    permission("write")
    await hold_review(db_session, saver)

    assert await run_confirm_job(confirm()) == "posted"
    assert await run_confirm_job(confirm()) == "ignored"
    assert gh.reviews.call_count == 1


@respx.mock
async def test_a_push_after_the_hold_makes_confirm_do_nothing(
    db_session: Session, saver: Saver
) -> None:
    gh = Pipeline(EVENT)
    permission("write")
    await hold_review(db_session, saver)
    runs.supersede_older(db_session, REPO, EVENT.number, "b" * 40)

    assert await run_confirm_job(confirm()) == "ignored"

    assert gh.reviews.call_count == 0
    assert run_row(db_session).status == runs.SUPERSEDED


@respx.mock
async def test_head_moved_after_claim_posts_nothing_and_cleans_up(
    db_session: Session, saver: Saver
) -> None:
    gh = Pipeline(EVENT, head_sha="c" * 40)  # GitHub's current head is a newer commit
    permission("write")
    await hold_review(db_session, saver)

    assert await run_confirm_job(confirm()) == "superseded"

    assert gh.reviews.call_count == 0
    assert run_row(db_session).status == runs.SUPERSEDED
    assert not saver.has_thread()


@respx.mock
async def test_retry_after_a_failed_post_posts_once(db_session: Session, saver: Saver) -> None:
    gh = Pipeline(EVENT)
    gh.reviews.mock(side_effect=[httpx.Response(500), httpx.Response(200, json={})])
    permission("write")
    await hold_review(db_session, saver)

    with pytest.raises(httpx.HTTPStatusError):
        await run_confirm_job(confirm())
    runs.revert_to_awaiting(db_session, RUN_ID)  # what the worker does after a failure
    assert saver.has_thread()  # kept for the retry

    assert await run_confirm_job(confirm()) == "posted"  # the graph had finished: no re-resume
    assert gh.reviews.call_count == 2  # one failed attempt, one real post
    assert run_row(db_session).status == runs.OK
    assert not saver.has_thread()


@respx.mock
async def test_retry_after_a_post_that_was_recorded_does_not_post_again(
    db_session: Session, saver: Saver
) -> None:
    gh = Pipeline(EVENT)
    permission("write")
    await hold_review(db_session, saver)
    assert await run_confirm_job(confirm()) == "posted"
    # The worker died after posting but before finishing: the run is back to waiting.
    db_session.execute(
        update(ReviewRun).values(status=runs.AWAITING_CONFIRMATION, finished_at=None)
    )
    db_session.commit()

    assert await run_confirm_job(confirm()) == "already_posted"
    assert gh.reviews.call_count == 1
    assert run_row(db_session).status == runs.OK


# --- through the worker ------------------------------------------------------------------------


@respx.mock
async def test_worker_runs_a_queued_confirm_job(
    db_session: Session, saver: Saver, drain: Drain
) -> None:
    gh = Pipeline(EVENT)
    permission("write")
    await hold_review(db_session, saver)
    db_session.add(Job(kind=CONFIRM_REVIEW, payload=confirm().model_dump()))
    db_session.commit()

    await drain()

    assert gh.reviews.call_count == 1
    assert db_session.scalars(select(Job)).one().status == "done"
    assert run_row(db_session).status == runs.OK


# --- 10.10: hardening --------------------------------------------------------------------------


def comment_count(gh: Pipeline) -> int:
    return gh.comments.call_count


@respx.mock
async def test_the_pr_author_cannot_confirm_their_own_review_even_with_admin_rights(
    db_session: Session, saver: Saver
) -> None:
    gh = Pipeline(EVENT)
    perm = permission("admin")
    await hold_review(db_session, saver)

    payload = confirm("maintainer").model_copy(update={"author_login": "Maintainer"})
    assert await run_confirm_job(payload) == "ignored"

    assert gh.reviews.call_count == 0 and perm.call_count == 0  # not even a permission lookup
    assert run_row(db_session).status == runs.AWAITING_CONFIRMATION
    assert comment_count(gh) == 1
    assert handlers.AUTHOR_COMMENT.encode() in gh.comments.calls[0].request.content


@respx.mock
async def test_an_unprivileged_user_gets_one_short_reply_not_silence_and_not_two(
    db_session: Session, saver: Saver
) -> None:
    gh = Pipeline(EVENT)
    permission("read")
    await hold_review(db_session, saver)

    assert await run_confirm_job(confirm()) == "ignored"
    assert await run_confirm_job(confirm()) == "ignored"  # e.g. a redelivery that slipped through

    assert comment_count(gh) == 1
    assert handlers.NOT_ALLOWED_COMMENT.encode() in gh.comments.calls[0].request.content
    assert runs.is_denied(db_session, RUN_ID, "maintainer")


@respx.mock
async def test_replies_stop_after_a_few_different_people_are_refused(
    db_session: Session, saver: Saver
) -> None:
    gh = Pipeline(EVENT)
    await hold_review(db_session, saver)
    for n in range(6):
        respx.get(PERMISSION_URL.replace("maintainer", f"user{n}")).mock(
            return_value=httpx.Response(200, json={"permission": "read"})
        )
        await run_confirm_job(confirm(f"user{n}"))
    assert comment_count(gh) == handlers.MAX_REFUSAL_REPLIES_PER_RUN
    assert runs.denial_count(db_session, RUN_ID) == 6


@respx.mock
async def test_a_failed_refusal_reply_changes_nothing(db_session: Session, saver: Saver) -> None:
    gh = Pipeline(EVENT)
    gh.comments.mock(return_value=httpx.Response(500))
    permission("none")
    await hold_review(db_session, saver)
    assert await run_confirm_job(confirm()) == "ignored"
    assert run_row(db_session).status == runs.AWAITING_CONFIRMATION
