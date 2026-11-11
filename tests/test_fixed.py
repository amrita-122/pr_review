"""10.16: a fixed finding is answered with "Fixed in <sha>" and its thread resolved."""

import json

import httpx
import pytest
import respx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import runs
from app.db.models import FindingRow
from app.jobs import handlers
from app.review.fake import review_file
from app.review.schemas import Finding
from tests.helpers import FILES, Pipeline
from tests.test_jobs import Drain, Enqueue, make_event, run_of

FIRST = make_event("a" * 40, "2026-11-10T10:00:00Z")
SECOND = make_event("b" * 40, "2026-11-10T10:05:00Z")
THIRD = make_event("c" * 40, "2026-11-10T10:10:00Z")
COMMENT_ID = 501
BASE = f"https://api.github.com/repos/{FIRST.owner}/{FIRST.repo}/pulls/{FIRST.number}"


class Setup:
    def __init__(self, gh: Pipeline, threads: list[dict]) -> None:
        self.gh = gh
        self.queries: list[str] = []
        self.reply = respx.post(f"{BASE}/comments/{COMMENT_ID}/replies").mock(
            return_value=httpx.Response(201, json={})
        )
        self.graphql = respx.post("https://api.github.com/graphql").mock(side_effect=self._graphql)
        self.threads = threads
        self.deletes = respx.delete(url__regex=r".*").mock(return_value=httpx.Response(204))

    def _graphql(self, request: httpx.Request) -> httpx.Response:
        query = json.loads(request.content)["query"]
        self.queries.append("resolve" if "resolveReviewThread" in query else "threads")
        if "resolveReviewThread" in query:
            return httpx.Response(200, json={"data": {"resolveReviewThread": {}}})
        page = {"pageInfo": {"hasNextPage": False, "endCursor": None}, "nodes": self.threads}
        return httpx.Response(
            200, json={"data": {"repository": {"pullRequest": {"reviewThreads": page}}}}
        )


def thread(comment_id: int = COMMENT_ID, resolved: bool = False) -> dict:
    return {
        "id": "PRRT_1",
        "isResolved": resolved,
        "comments": {"nodes": [{"databaseId": comment_id}]},
    }


@pytest.fixture
def reviewer_state(monkeypatch: pytest.MonkeyPatch) -> dict:
    """Which findings the fake model reports on the next push."""
    state: dict = {"report": True, "failing": False}

    async def reviewer(path: str, numbered: str) -> list[Finding]:
        if state["failing"]:
            raise RuntimeError("model down")
        return review_file(path, numbered) if state["report"] else []

    monkeypatch.setattr(handlers, "build_reviewers", lambda: reviewer)
    return state


def sha_head(gh: Pipeline, event) -> None:  # noqa: ANN001
    gh.head.mock(return_value=httpx.Response(200, json={"head": {"sha": event.head_sha}}))


async def first_push(gh: Pipeline, enqueue: Enqueue, drain: Drain) -> None:
    gh.reviews.mock(return_value=httpx.Response(200, json={"id": 77}))
    respx.get(f"{BASE}/reviews/77/comments").mock(
        return_value=httpx.Response(
            200, json=[{"id": COMMENT_ID, "node_id": "N1", "path": "app/db.py", "line": 2}]
        )
    )
    enqueue(FIRST)
    await drain()
    assert gh.reviews.call_count == 1


def finding_rows(db: Session) -> list[FindingRow]:
    db.expire_all()
    return list(db.scalars(select(FindingRow).order_by(FindingRow.id)))


@respx.mock
async def test_a_fixed_finding_is_answered_and_its_thread_resolved(
    db_session: Session, enqueue: Enqueue, drain: Drain, reviewer_state: dict
) -> None:
    gh = Pipeline(FIRST)
    setup = Setup(gh, [thread()])
    await first_push(gh, enqueue, drain)

    reviewer_state["report"] = False  # the bug is gone in the second push
    sha_head(gh, SECOND)
    enqueue(SECOND)
    await drain()

    assert setup.reply.call_count == 1
    assert "Fixed in `bbbbbbb`" in json.loads(setup.reply.calls[0].request.content)["body"]
    assert setup.queries == ["threads", "resolve"]
    assert setup.deletes.call_count == 0  # comments are never deleted
    first_row = finding_rows(db_session)[0]
    assert first_row.fixed_in_sha == "b" * 12 and first_row.posted is True


@respx.mock
async def test_a_finding_that_is_still_there_is_left_alone(
    db_session: Session, enqueue: Enqueue, drain: Drain, reviewer_state: dict
) -> None:
    gh = Pipeline(FIRST)
    setup = Setup(gh, [thread()])
    await first_push(gh, enqueue, drain)

    sha_head(gh, SECOND)
    enqueue(SECOND)  # the model reports the same bug again
    await drain()

    assert setup.reply.call_count == 0 and setup.queries == []
    assert finding_rows(db_session)[0].fixed_in_sha is None


@respx.mock
async def test_nothing_is_closed_when_part_of_the_review_failed(
    db_session: Session,
    enqueue: Enqueue,
    drain: Drain,
    reviewer_state: dict,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A silent or broken model is not evidence of a fix."""
    gh = Pipeline(FIRST)
    setup = Setup(gh, [thread()])
    await first_push(gh, enqueue, drain)

    other = {"filename": "app/other.py", "status": "modified", "patch": FILES[0]["patch"]}
    gh.files.mock(return_value=httpx.Response(200, json=[*FILES, other]))

    async def flaky(path: str, numbered: str) -> list[Finding]:
        if path == "app/other.py":
            raise RuntimeError("boom")
        return []

    monkeypatch.setattr(handlers, "build_reviewers", lambda: flaky)
    sha_head(gh, SECOND)
    enqueue(SECOND)
    await drain()

    assert setup.reply.call_count == 0
    assert finding_rows(db_session)[0].fixed_in_sha is None


@respx.mock
async def test_a_file_that_was_not_reviewed_this_time_is_not_judged(
    db_session: Session, enqueue: Enqueue, drain: Drain, reviewer_state: dict
) -> None:
    gh = Pipeline(FIRST)
    setup = Setup(gh, [thread()])
    await first_push(gh, enqueue, drain)

    only_lock = [f for f in FILES if f["filename"] == "uv.lock"]
    gh.files.mock(return_value=httpx.Response(200, json=only_lock))  # app/db.py not in this push
    reviewer_state["report"] = False
    sha_head(gh, SECOND)
    enqueue(SECOND)
    await drain()

    assert setup.reply.call_count == 0
    assert finding_rows(db_session)[0].fixed_in_sha is None


@respx.mock
async def test_a_failed_reply_is_retried_on_the_next_push_and_never_fails_the_job(
    db_session: Session, enqueue: Enqueue, drain: Drain, reviewer_state: dict
) -> None:
    gh = Pipeline(FIRST)
    setup = Setup(gh, [thread()])
    await first_push(gh, enqueue, drain)
    setup.reply.mock(return_value=httpx.Response(500))

    reviewer_state["report"] = False
    sha_head(gh, SECOND)
    enqueue(SECOND)
    await drain()
    assert run_of(db_session, "b" * 40).status == runs.OK  # the review itself is fine
    assert finding_rows(db_session)[0].fixed_in_sha is None  # claim released

    setup.reply.mock(return_value=httpx.Response(201, json={}))
    sha_head(gh, THIRD)
    enqueue(THIRD)
    await drain()
    assert finding_rows(db_session)[0].fixed_in_sha == "c" * 12


@respx.mock
async def test_an_already_resolved_thread_is_not_resolved_again(
    db_session: Session, enqueue: Enqueue, drain: Drain, reviewer_state: dict
) -> None:
    gh = Pipeline(FIRST)
    setup = Setup(gh, [thread(resolved=True)])
    await first_push(gh, enqueue, drain)
    reviewer_state["report"] = False
    sha_head(gh, SECOND)
    enqueue(SECOND)
    await drain()
    assert setup.reply.call_count == 1 and setup.queries == ["threads"]


@respx.mock
async def test_a_bug_that_comes_back_after_a_fix_is_a_new_finding(
    db_session: Session, enqueue: Enqueue, drain: Drain, reviewer_state: dict
) -> None:
    gh = Pipeline(FIRST)
    Setup(gh, [thread()])
    await first_push(gh, enqueue, drain)
    reviewer_state["report"] = False
    sha_head(gh, SECOND)
    enqueue(SECOND)
    await drain()

    reviewer_state["report"] = True  # a regression
    gh.reviews.mock(return_value=httpx.Response(200, json={}))
    sha_head(gh, THIRD)
    enqueue(THIRD)
    await drain()
    assert [c["path"] for c in gh.sent_review(2)["comments"]] == ["app/db.py"]
