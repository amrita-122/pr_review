"""Phase 8 plumbing: run states, claims, permission lookup, confirm-job enqueue, checkpoints."""

from datetime import UTC, datetime, timedelta

import httpx
import pytest
import respx
from langgraph.checkpoint.memory import InMemorySaver
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import runs
from app.db.models import Job
from app.github.client import get_collaborator_permission
from app.jobs.payload import CONFIRM_REVIEW, ConfirmJobPayload
from app.jobs.queue import enqueue_confirm, enqueue_review
from app.review.checkpoint import checkpoint_conninfo, delete_thread
from tests.test_jobs import make_event

NOW = datetime(2026, 11, 4, 12, 0, tzinfo=UTC)


def held_run(db: Session, sha: str | None = None) -> str:
    """A run that finished reviewing and is waiting for /confirm."""
    event = make_event(sha)
    enqueue_review(db, event, get_settings())
    db.commit()
    run = runs.get_by_key(db, f"{event.owner}/{event.repo}", event.number, event.head_sha)
    assert run is not None
    assert runs.mark_running(db, run.id, prompt_version="p", model="m")
    assert runs.mark_awaiting(db, run.id, installation_id=7)
    return run.id


def confirm_payload(run_id: str, login: str = "maintainer") -> ConfirmJobPayload:
    event = make_event()
    return ConfirmJobPayload(
        run_id=run_id,
        installation_id=event.installation_id,
        owner=event.owner,
        repo=event.repo,
        number=event.number,
        comment_id=99,
        commenter_login=login,
    )


# --- state transitions ---------------------------------------------------------------------


def test_mark_awaiting_records_installation_and_time(db_session: Session) -> None:
    run_id = held_run(db_session)
    run = runs.get_run(db_session, run_id)
    assert run is not None
    assert run.status == runs.AWAITING_CONFIRMATION
    assert run.installation_id == 7
    assert run.awaiting_since is not None


def test_superseded_run_cannot_become_pending(db_session: Session) -> None:
    event = make_event()
    enqueue_review(db_session, event, get_settings())
    db_session.commit()
    run = runs.get_by_key(db_session, f"{event.owner}/{event.repo}", event.number, event.head_sha)
    assert run is not None
    runs.mark_superseded(db_session, run.id)
    assert runs.mark_awaiting(db_session, run.id, installation_id=7) is False


def test_confirmation_is_claimed_exactly_once(db_session: Session) -> None:
    run_id = held_run(db_session)
    assert runs.claim_confirmation(db_session, run_id, login="alice", now=NOW) is True
    assert runs.claim_confirmation(db_session, run_id, login="bob", now=NOW) is False
    run = runs.get_run(db_session, run_id)
    assert run is not None
    assert (run.status, run.confirmed_by) == (runs.CONFIRMING, "alice")


def test_failed_confirm_returns_run_to_awaiting(db_session: Session) -> None:
    run_id = held_run(db_session)
    runs.claim_confirmation(db_session, run_id, login="alice", now=NOW)
    runs.revert_to_awaiting(db_session, run_id)
    run = runs.get_run(db_session, run_id)
    assert run is not None
    assert (run.status, run.confirmed_by) == (runs.AWAITING_CONFIRMATION, None)
    assert runs.claim_confirmation(db_session, run_id, login="bob", now=NOW) is True


def test_newer_push_supersedes_a_held_run(db_session: Session) -> None:
    run_id = held_run(db_session, "a" * 40)
    enqueue_review(db_session, make_event("b" * 40), get_settings())
    db_session.commit()
    old = runs.get_run(db_session, run_id)
    assert old is not None
    assert old.status == runs.SUPERSEDED
    assert runs.claim_confirmation(db_session, run_id, login="alice", now=NOW) is False


def test_pending_comment_is_claimed_once(db_session: Session) -> None:
    run_id = held_run(db_session)
    assert runs.claim_pending_comment(db_session, run_id) is True
    assert runs.claim_pending_comment(db_session, run_id) is False


def test_expiry_only_takes_old_held_runs_and_loses_to_a_confirm(db_session: Session) -> None:
    run_id = held_run(db_session)
    assert runs.list_expired(db_session, datetime.now(UTC) - timedelta(hours=1)) == []
    due = runs.list_expired(db_session, datetime.now(UTC) + timedelta(hours=1))
    assert [r.id for r in due] == [run_id]

    runs.claim_confirmation(db_session, run_id, login="alice", now=NOW)  # confirm wins the race
    assert runs.expire_run(db_session, run_id) is False

    runs.revert_to_awaiting(db_session, run_id)
    assert runs.expire_run(db_session, run_id) is True
    assert runs.claim_expired_comment(db_session, run_id) is True
    assert runs.claim_expired_comment(db_session, run_id) is False
    assert runs.claim_confirmation(db_session, run_id, login="alice", now=NOW) is False


# --- enqueue_confirm -----------------------------------------------------------------------


def test_confirm_job_is_queued_for_a_held_run(db_session: Session) -> None:
    run_id = held_run(db_session)
    assert enqueue_confirm(db_session, confirm_payload(run_id), get_settings()) is True
    db_session.commit()
    job = db_session.scalars(select(Job).where(Job.kind == CONFIRM_REVIEW)).one()
    assert job.payload["run_id"] == run_id
    assert job.payload["commenter_login"] == "maintainer"


def test_no_confirm_job_without_a_held_run(db_session: Session) -> None:
    assert enqueue_confirm(db_session, confirm_payload("nope"), get_settings()) is False
    run_id = held_run(db_session)
    runs.claim_confirmation(db_session, run_id, login="alice", now=NOW)  # no longer awaiting
    assert enqueue_confirm(db_session, confirm_payload(run_id), get_settings()) is False
    assert db_session.scalars(select(Job).where(Job.kind == CONFIRM_REVIEW)).first() is None


def test_confirm_payload_rejects_a_login_that_could_escape_the_url_path() -> None:
    for bad in ("../admin", "a/b", "x" * 40, "", "name with space", "a?b"):
        with pytest.raises(ValidationError):
            confirm_payload("r", login=bad)


# --- collaborator permission ---------------------------------------------------------------

PERM_URL = "https://api.github.com/repos/o/r/collaborators/{}/permission"


@respx.mock
async def test_permission_is_returned_as_github_reports_it() -> None:
    route = respx.get(PERM_URL.format("alice")).mock(
        return_value=httpx.Response(200, json={"permission": "write", "role_name": "write"})
    )
    assert await get_collaborator_permission("tok", "o", "r", "alice") == "write"
    assert route.calls[0].request.headers["Authorization"] == "Bearer tok"


@respx.mock
async def test_unknown_user_is_none_and_other_errors_raise() -> None:
    respx.get(PERM_URL.format("ghost")).mock(return_value=httpx.Response(404))
    assert await get_collaborator_permission("tok", "o", "r", "ghost") is None
    respx.get(PERM_URL.format("alice")).mock(return_value=httpx.Response(403))
    with pytest.raises(httpx.HTTPStatusError):  # not "no permission": the caller must retry
        await get_collaborator_permission("tok", "o", "r", "alice")


async def test_bad_login_never_reaches_the_network() -> None:
    with pytest.raises(ValueError, match="invalid GitHub login"):
        await get_collaborator_permission("tok", "o", "r", "../../orgs")


# --- checkpoint helpers --------------------------------------------------------------------


def test_checkpoint_conninfo_is_plain_libpq_with_timeouts(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgres://u:p@h:5432/db")
    get_settings.cache_clear()
    url = checkpoint_conninfo(get_settings())
    assert url.startswith("postgresql://u:p@h:5432/db?")
    assert "+psycopg" not in url
    assert "connect_timeout=5" in url
    assert "statement_timeout" in url


async def test_delete_thread_succeeds_and_survives_errors() -> None:
    assert await delete_thread(InMemorySaver(), "run-1") is True

    class Broken(InMemorySaver):
        async def adelete_thread(self, thread_id: str) -> None:
            raise RuntimeError("db down")

    assert await delete_thread(Broken(), "run-1") is False  # logged, never raised
