"""/review (10.30): a maintainer brings back a failed, superseded or rate-limited review."""

from collections.abc import Awaitable, Callable

import httpx
import pytest
import respx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import runs
from app.db.models import Job, ReviewRun
from app.github.webhooks import command_of
from app.jobs.payload import CONFIRM_REVIEW, REVIEW_PR, ConfirmJobPayload, ReviewJobPayload
from tests.helpers import token_response
from tests.test_confirm_webhook import NUMBER, REPO, comment
from tests.test_jobs import make_event, settings_env
from tests.test_webhook_signatures import post_signed

Drain = Callable[[], Awaitable[None]]
SHA = "a" * 40


def make_run(db: Session, status: str, run_id: str = "run1") -> None:
    runs.create_queued(
        db, run_id=run_id, repo=REPO, pr_number=NUMBER, head_sha=SHA, prompt_version="v", model="m"
    )
    run = db.get(ReviewRun, run_id)
    assert run is not None
    run.status = status
    db.commit()


@pytest.mark.parametrize(
    ("body", "expected"),
    [("/review", "review"), (" /review\n", "review"), ("/review now", None), ("/Review", None)],
)
def test_command_parsing_is_exact(body: str, expected: str | None) -> None:
    assert command_of(body) == expected


@pytest.mark.parametrize("status", ["failed", "superseded", "skipped"])
def test_review_on_a_rerunnable_run_queues_a_job(db_session: Session, status: str) -> None:
    make_run(db_session, status)
    resp = post_signed(comment(body="/review"), event="issue_comment")
    assert resp.json() == {"status": "accepted"}
    job = db_session.scalars(select(Job)).one()
    assert job.kind == CONFIRM_REVIEW and job.payload["command"] == "review"
    assert job.payload["run_id"] == "run1"


@pytest.mark.parametrize("status", ["ok", "running", "queued", "awaiting_confirmation"])
def test_review_on_a_healthy_or_busy_run_costs_nothing(db_session: Session, status: str) -> None:
    make_run(db_session, status)
    assert post_signed(comment(body="/review"), event="issue_comment").json() == {
        "status": "ignored"
    }
    assert db_session.scalars(select(Job)).all() == []


def test_review_with_no_run_or_when_disabled_is_ignored(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert post_signed(comment(body="/review"), event="issue_comment").json() == {
        "status": "ignored"
    }
    make_run(db_session, "failed")
    settings_env(monkeypatch, REVIEW_COMMAND_ENABLED="false")
    assert post_signed(comment(body="/review"), event="issue_comment").json() == {
        "status": "ignored"
    }
    assert db_session.scalars(select(Job)).all() == []


def test_a_bot_cannot_trigger_review(db_session: Session) -> None:
    make_run(db_session, "failed")
    body = comment(body="/review", user={"login": "review-bot[bot]", "type": "Bot"})
    assert post_signed(body, event="issue_comment").json() == {"status": "ignored"}


def test_rate_limited_pr_leaves_a_skipped_run_that_review_can_revive(
    db_session: Session, monkeypatch: pytest.MonkeyPatch, enqueue: Callable[..., None]
) -> None:
    settings_env(monkeypatch, MAX_RUNS_PER_AUTHOR_PER_HOUR="1")
    enqueue(make_event(sha="a" * 40))
    enqueue(make_event(sha="b" * 40))
    statuses = sorted(r.status for r in db_session.scalars(select(ReviewRun)))
    assert statuses == ["queued", "skipped"] and len(db_session.scalars(select(Job)).all()) == 1


# --- the worker side ---------------------------------------------------------------------------


def add_job(db: Session, author: str | None = None) -> None:
    payload = ConfirmJobPayload(
        run_id="run1",
        installation_id=1,
        owner=REPO.split("/")[0],
        repo=REPO.split("/")[1],
        number=NUMBER,
        comment_id=5,
        commenter_login="maint",
        author_login=author,
        command="review",
    )
    db.add(Job(kind=CONFIRM_REVIEW, payload=payload.model_dump()))
    db.commit()


def mock_github(permission: str, head: str = SHA) -> None:
    owner, repo = REPO.split("/")
    respx.post(url__regex=r".*/access_tokens").mock(return_value=token_response())
    respx.get(f"https://api.github.com/repos/{REPO}/collaborators/maint/permission").mock(
        return_value=httpx.Response(200, json={"permission": permission})
    )
    respx.get(f"https://api.github.com/repos/{owner}/{repo}/pulls/{NUMBER}").mock(
        return_value=httpx.Response(200, json={"head": {"sha": head}, "base": {"sha": "b" * 40}})
    )
    respx.post(url__regex=r".*/issues/\d+/comments").mock(return_value=httpx.Response(201, json={}))


@respx.mock
async def test_allowed_maintainer_requeues_the_run_with_a_review_job(
    db_session: Session, drain: Drain
) -> None:
    make_run(db_session, "failed")
    add_job(db_session)
    mock_github("write")
    await drain()
    db_session.expire_all()
    assert db_session.get(ReviewRun, "run1").status == "queued"  # type: ignore[union-attr]
    job = db_session.scalars(select(Job).where(Job.kind == REVIEW_PR)).one()
    payload = ReviewJobPayload.model_validate(job.payload)
    assert (payload.run_id, payload.head_sha, payload.base_sha) == ("run1", SHA, "b" * 40)


@respx.mock
async def test_read_only_user_is_refused_and_nothing_is_queued(
    db_session: Session, drain: Drain
) -> None:
    make_run(db_session, "failed")
    add_job(db_session)
    mock_github("read")
    await drain()
    db_session.expire_all()
    assert db_session.get(ReviewRun, "run1").status == "failed"  # type: ignore[union-attr]
    assert db_session.scalars(select(Job).where(Job.kind == REVIEW_PR)).all() == []


@respx.mock
async def test_the_author_cannot_use_review_to_skip_the_rate_limit(
    db_session: Session, drain: Drain
) -> None:
    make_run(db_session, "skipped")
    add_job(db_session, author="maint")
    mock_github("admin")
    await drain()
    db_session.expire_all()
    assert db_session.get(ReviewRun, "run1").status == "skipped"  # type: ignore[union-attr]


@respx.mock
async def test_a_run_for_an_old_head_is_not_revived(db_session: Session, drain: Drain) -> None:
    make_run(db_session, "superseded")
    add_job(db_session)
    mock_github("write", head="c" * 40)  # the PR moved on: the new push has its own run
    await drain()
    db_session.expire_all()
    assert db_session.get(ReviewRun, "run1").status == "superseded"  # type: ignore[union-attr]


def test_setting_defaults() -> None:
    assert get_settings().review_command_enabled is True
