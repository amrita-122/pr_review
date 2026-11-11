"""10.14: GitHub's review, comment and node ids are stored with each posted finding."""

import httpx
import respx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import runs
from app.db.models import FindingRow
from app.github.client import post_review
from tests.helpers import Pipeline
from tests.test_jobs import Drain, Enqueue, make_event

EVENT = make_event()
REVIEW_COMMENTS = "https://api.github.com/repos/{owner}/{repo}/pulls/{number}/reviews/77/comments"


def comments_url(event=EVENT) -> str:  # noqa: ANN001
    return REVIEW_COMMENTS.format(owner=event.owner, repo=event.repo, number=event.number)


def rows(db: Session) -> list[FindingRow]:
    db.expire_all()
    return list(db.scalars(select(FindingRow).order_by(FindingRow.id)))


@respx.mock
async def test_posting_a_review_stores_the_ids_on_the_finding(
    db_session: Session, enqueue: Enqueue, drain: Drain
) -> None:
    gh = Pipeline(EVENT)
    gh.reviews.mock(return_value=httpx.Response(200, json={"id": 77, "node_id": "PRR_kw77"}))
    listing = respx.get(comments_url()).mock(
        return_value=httpx.Response(
            200, json=[{"id": 501, "node_id": "PRRC_kw501", "path": "app/db.py", "line": 2}]
        )
    )
    enqueue(EVENT)
    await drain()

    assert listing.call_count == 1
    (row,) = rows(db_session)
    assert (row.posted, row.github_review_id, row.github_comment_id, row.github_node_id) == (
        True,
        77,
        501,
        "PRRC_kw501",
    )


@respx.mock
async def test_ids_are_best_effort_a_failed_lookup_never_fails_the_job(
    db_session: Session, enqueue: Enqueue, drain: Drain
) -> None:
    gh = Pipeline(EVENT)
    gh.reviews.mock(return_value=httpx.Response(200, json={"id": 77}))
    respx.get(comments_url()).mock(return_value=httpx.Response(500))
    enqueue(EVENT)
    await drain()

    assert gh.reviews.call_count == 1
    (row,) = rows(db_session)
    assert row.posted is True and row.github_comment_id is None
    from tests.test_jobs import run_of

    assert run_of(db_session).status == runs.OK  # the review is posted; the run is fine


@respx.mock
async def test_the_summary_only_fallback_records_the_review_id_without_comment_ids(
    db_session: Session, enqueue: Enqueue, drain: Drain
) -> None:
    gh = Pipeline(EVENT)
    gh.reviews.mock(
        side_effect=[httpx.Response(422, json={}), httpx.Response(200, json={"id": 77})]
    )
    enqueue(EVENT)
    await drain()

    (row,) = rows(db_session)
    assert (row.github_review_id, row.github_comment_id) == (77, None)


@respx.mock
async def test_post_review_tolerates_a_response_without_ids() -> None:
    from app.github.client import ReviewComment

    Pipeline(EVENT)
    posted = await post_review(
        "tok",
        EVENT.owner,
        EVENT.repo,
        EVENT.number,
        EVENT.head_sha,
        "body",
        [ReviewComment(path="a.py", line=1, body="b")],
    )
    assert posted.id is None and posted.comments == []


def test_ids_only_go_to_the_matching_finding_and_are_never_overwritten(db_session: Session) -> None:
    runs.create_queued(
        db_session, run_id="r1", repo="o/r", pr_number=1, head_sha="a" * 40,
        prompt_version="v", model="m",
    )  # fmt: skip

    def row(file: str, line: int, severity: str, posted: bool) -> FindingRow:
        return FindingRow(
            run_id="r1", file=file, line=line, severity=severity, category="bug", title="t",
            posted=posted,
        )  # fmt: skip

    db_session.add_all(
        [row("a.py", 3, "high", True), row("b.py", 9, "low", True), row("c.py", 1, "low", False)]
    )
    db_session.commit()

    runs.save_github_ids(
        db_session, "r1", review_id=5, comments=[("b.py", 9, 900, "N9"), ("zzz.py", 1, 1, "N1")]
    )
    runs.save_github_ids(db_session, "r1", review_id=6, comments=[("b.py", 9, 901, "N10")])

    by_file = {r.file: r for r in rows(db_session)}
    assert by_file["b.py"].github_comment_id == 900  # first write wins
    assert (
        by_file["a.py"].github_comment_id is None and by_file["a.py"].github_review_id == 5
    )  # first review wins
    assert by_file["c.py"].github_review_id is None  # not posted: no ids
