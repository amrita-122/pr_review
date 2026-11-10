"""10.3: a closed or merged PR gets no more reviews, comments or confirmations."""

import json
import uuid

import pytest
import respx
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import runs
from app.db.models import Job
from app.github.webhooks import PullRequestEvent
from app.jobs.handlers import purge_closed_checkpoints
from tests.test_confirmation_state import RecordingSaver, saver_factory
from tests.test_jobs import RAW, make_event
from tests.test_webhook_signatures import post_signed

REPO = f"{RAW['repository']['owner']['login']}/{RAW['repository']['name']}"
NUMBER = RAW["number"]
LATER = "2099-01-01T00:00:00Z"


@pytest.fixture(autouse=True)
def confirmation_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CONFIRMATION_ENABLED", "true")
    get_settings.cache_clear()


def closed_body(updated_at: str = LATER, merged: bool = False) -> bytes:
    raw = json.loads(json.dumps(RAW))
    raw["action"] = "closed"
    raw["pull_request"]["updated_at"] = updated_at
    raw["pull_request"]["merged"] = merged
    return json.dumps(raw).encode()


def make_run(db: Session, run_id: str, *, held: bool, stamp: str | None = None) -> None:
    event: PullRequestEvent = make_event(run_id[0] * 40, stamp)
    runs.create_queued(
        db, run_id=run_id, repo=REPO, pr_number=NUMBER, head_sha=event.head_sha,
        prompt_version="v", model="m", head_updated_at=event.head_updated_at,
    )  # fmt: skip
    if held:
        assert runs.mark_running(db, run_id, prompt_version="v", model="m")
        assert runs.mark_awaiting(db, run_id, installation_id=1)
    db.commit()


def status_of(db: Session, run_id: str) -> str:
    db.expire_all()
    run = runs.get_run(db, run_id)
    assert run is not None
    return run.status


def count(db: Session, model: type) -> int:
    return db.scalar(select(func.count()).select_from(model)) or 0


def test_closing_expires_a_held_run_quietly_and_queues_its_checkpoint_for_deletion(
    db_session: Session,
) -> None:
    make_run(db_session, "a1", held=True, stamp="2026-11-10T10:00:00Z")
    resp = post_signed(closed_body())
    assert (resp.status_code, resp.json()) == (202, {"status": "closed"})
    assert status_of(db_session, "a1") == runs.EXPIRED
    run = runs.get_run(db_session, "a1")
    assert run is not None
    assert run.expired_comment_posted is True  # nothing will ever be posted to the dead PR
    assert run.purge_checkpoint is True
    assert count(db_session, Job) == 0  # a close queues no work at all


def test_merged_pr_is_handled_like_a_closed_one(db_session: Session) -> None:
    make_run(db_session, "a1", held=True, stamp="2026-11-10T10:00:00Z")
    post_signed(closed_body(merged=True))
    assert status_of(db_session, "a1") == runs.EXPIRED


def test_confirm_on_a_closed_pr_queues_nothing(db_session: Session) -> None:
    from tests.test_confirm_webhook import comment, hold

    hold(db_session, "h1")  # the fixture comment's PR
    db_session.commit()
    comment_raw = json.loads(comment())
    close = json.loads(closed_body())
    close["number"] = comment_raw["issue"]["number"]
    close["repository"] = comment_raw["repository"]
    post_signed(json.dumps(close).encode())
    assert post_signed(comment(), event="issue_comment").json() == {"status": "ignored"}
    assert count(db_session, Job) == 0


def test_closing_supersedes_queued_and_running_runs(db_session: Session) -> None:
    make_run(db_session, "a1", held=False, stamp="2026-11-10T10:00:00Z")
    make_run(db_session, "b1", held=False, stamp="2026-11-10T10:00:01Z")
    runs.mark_running(db_session, "b1", prompt_version="v", model="m")
    post_signed(closed_body())
    assert status_of(db_session, "a1") == status_of(db_session, "b1") == runs.SUPERSEDED


def test_a_late_close_does_not_kill_a_run_from_a_later_push(db_session: Session) -> None:
    """Close at 10:00:00 arrives after a reopen + push stamped 10:05: that run stays alive."""
    make_run(db_session, "a1", held=False, stamp="2026-11-10T10:05:00Z")
    post_signed(closed_body(updated_at="2026-11-10T10:00:00Z"))
    assert status_of(db_session, "a1") == runs.QUEUED


def test_finished_runs_are_left_alone(db_session: Session) -> None:
    make_run(db_session, "a1", held=False, stamp="2026-11-10T10:00:00Z")
    runs.finish_run(db_session, "a1", status=runs.OK)
    post_signed(closed_body())
    assert status_of(db_session, "a1") == runs.OK


def test_a_redelivered_close_is_a_duplicate(db_session: Session) -> None:
    delivery = str(uuid.uuid4())
    first = post_signed(closed_body(), delivery=delivery)
    second = post_signed(closed_body(), delivery=delivery)
    assert (first.json(), second.json()) == ({"status": "closed"}, {"status": "duplicate"})


@respx.mock  # any GitHub call would fail the test: closing must not talk to GitHub
async def test_checkpoint_of_a_closed_pr_is_deleted_once_and_nothing_is_posted(
    db_session: Session,
) -> None:
    make_run(db_session, "a1", held=True, stamp="2026-11-10T10:00:00Z")
    post_signed(closed_body())
    saver = RecordingSaver()

    assert await purge_closed_checkpoints(get_settings(), saver_factory(saver)) == 1
    assert await purge_closed_checkpoints(get_settings(), saver_factory(saver)) == 0
    assert saver.deleted == ["a1"]
    db_session.expire_all()
    run = runs.get_run(db_session, "a1")
    assert run is not None and run.purge_checkpoint is False


async def test_failed_checkpoint_deletion_is_retried_on_the_next_sweep(
    db_session: Session,
) -> None:
    make_run(db_session, "a1", held=True, stamp="2026-11-10T10:00:00Z")
    post_signed(closed_body())

    class Broken(RecordingSaver):
        async def adelete_thread(self, thread_id: str) -> None:
            raise RuntimeError("db down")

    assert await purge_closed_checkpoints(get_settings(), saver_factory(Broken())) == 0
    good = RecordingSaver()
    assert await purge_closed_checkpoints(get_settings(), saver_factory(good)) == 1
    assert good.deleted == ["a1"]
