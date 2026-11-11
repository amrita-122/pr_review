"""Incremental review (10.31): only files changed since the last reviewed commit."""

from datetime import UTC, datetime

import httpx
import pytest
import respx
from sqlalchemy.orm import Session

from app.db import runs
from app.db.models import ReviewRun
from app.github.client import changed_files_between
from tests.helpers import Pipeline
from tests.test_jobs import Drain, Enqueue, make_event, settings_env

PREV = "1" * 40


def compare_url(head: str) -> str:
    return f"https://api.github.com/repos/o/r/compare/{PREV}...{head}"


@respx.mock
async def test_compare_returns_the_changed_names() -> None:
    respx.get(compare_url("h")).mock(
        return_value=httpx.Response(
            200, json={"status": "ahead", "files": [{"filename": "a.py"}, {"filename": "b/c.py"}]}
        )
    )
    assert await changed_files_between("t", "o", "r", PREV, "h") == {"a.py", "b/c.py"}


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(404),
        httpx.Response(200, json={"status": "diverged", "files": []}),
        httpx.Response(200, json={"status": "ahead", "files": [{"filename": "x"}] * 300}),
        httpx.Response(200, json={"status": "ahead"}),
        httpx.Response(200, text="not json"),
    ],
)
@respx.mock
async def test_anything_uncertain_means_review_everything(response: httpx.Response) -> None:
    respx.get(compare_url("h")).mock(return_value=response)
    assert await changed_files_between("t", "o", "r", PREV, "h") is None


@respx.mock
async def test_compare_network_error_means_review_everything() -> None:
    respx.get(compare_url("h")).mock(side_effect=httpx.ConnectError("down"))
    assert await changed_files_between("t", "o", "r", PREV, "h") is None


def earlier_run(db: Session, event_repo: str, number: int) -> None:
    runs.create_queued(
        db, run_id="prev", repo=event_repo, pr_number=number, head_sha=PREV,
        prompt_version="v", model="m",
    )  # fmt: skip
    run = db.get(ReviewRun, "prev")
    assert run is not None
    run.status, run.posted_at = runs.OK, datetime.now(UTC)
    db.commit()


@respx.mock
async def test_flag_on_skips_files_not_touched_since_the_last_reviewed_commit(
    db_session: Session, enqueue: Enqueue, drain: Drain, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings_env(monkeypatch, INCREMENTAL_REVIEW_ENABLED="true")
    event = make_event()
    earlier_run(db_session, f"{event.owner}/{event.repo}", event.number)
    gh = Pipeline(event)
    respx.get(url__regex=r".*/compare/.*").mock(
        return_value=httpx.Response(
            200, json={"status": "ahead", "files": [{"filename": "uv.lock"}]}
        )
    )
    enqueue(event)
    await drain()
    sent = gh.sent_review()
    assert sent["comments"] == []  # app/db.py (the only file with a finding) was not re-reviewed
    assert "Incremental review" in sent["body"]


@respx.mock
async def test_flag_off_or_unknown_history_reviews_everything(
    db_session: Session, enqueue: Enqueue, drain: Drain, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings_env(monkeypatch, INCREMENTAL_REVIEW_ENABLED="true")
    event = make_event()
    earlier_run(db_session, f"{event.owner}/{event.repo}", event.number)
    gh = Pipeline(event)
    respx.get(url__regex=r".*/compare/.*").mock(return_value=httpx.Response(404))  # force-push
    enqueue(event)
    await drain()
    sent = gh.sent_review()
    assert [c["path"] for c in sent["comments"]] == ["app/db.py"]
    assert "Incremental review" not in sent["body"]
