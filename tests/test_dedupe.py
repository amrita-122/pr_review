"""10.15: a finding already commented on an earlier push is not posted again."""

import httpx
import pytest
import respx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import runs
from app.db.models import FindingRow
from app.review.dedupe import ALREADY_POSTED, LINE_TOLERANCE, KnownFinding, drop_repeats, is_repeat
from app.review.render import render_summary
from app.review.schemas import Category, Finding, Severity
from tests.helpers import Pipeline
from tests.test_jobs import Drain, Enqueue, make_event, run_of


def finding(line: int = 10, path: str = "a.py", category: Category = Category.INJECTION) -> Finding:
    return Finding(
        file_path=path,
        line=line,
        severity=Severity.HIGH,
        category=category,
        title="t",
        rationale="r",
    )


KNOWN = [KnownFinding(file="a.py", category="injection", line=10)]


@pytest.mark.parametrize(
    ("changes", "expected"),
    [
        ({}, True),
        ({"line": 10 + LINE_TOLERANCE}, True),  # the code moved a few lines
        ({"line": 10 - LINE_TOLERANCE}, True),
        ({"line": 10 + LINE_TOLERANCE + 1}, False),  # too far: a different spot
        ({"path": "b.py"}, False),  # another file
        ({"category": Category.SECRETS}, False),  # another kind of problem on the same lines
    ],
)
def test_what_counts_as_the_same_finding(changes: dict, expected: bool) -> None:
    assert is_repeat(finding(**changes), KNOWN) is expected


def test_drop_repeats_splits_new_from_repeated_with_a_reason() -> None:
    new, repeated = finding(line=200), finding(line=11)
    fresh, dropped = drop_repeats([repeated, new], KNOWN)
    assert fresh == [new] and dropped == [(repeated, ALREADY_POSTED)]
    assert drop_repeats([new], []) == ([new], [])


def test_summary_never_calls_a_pr_with_open_findings_clean() -> None:
    text = render_summary(2, [], [], repeated=2)
    assert "No findings." not in text and "No new findings." in text
    assert "2 finding(s) from an earlier push are still open and not repeated." in text


# --- end to end: two pushes, the same bug ----------------------------------------------------


def two_pushes() -> tuple:  # noqa: ANN201
    return (
        make_event("a" * 40, "2026-11-10T10:00:00Z"),
        make_event("b" * 40, "2026-11-10T10:05:00Z"),
    )


@respx.mock
async def test_a_second_push_does_not_comment_on_the_same_bug_again(
    db_session: Session, enqueue: Enqueue, drain: Drain
) -> None:
    first, second = two_pushes()
    gh = Pipeline(first)
    enqueue(first)
    await drain()
    assert [c["path"] for c in gh.sent_review(0)["comments"]] == ["app/db.py"]

    gh.head.mock(return_value=httpx.Response(200, json={"head": {"sha": second.head_sha}}))
    enqueue(second)  # same bug, still there
    await drain()

    assert gh.reviews.call_count == 2
    again = gh.sent_review(1)
    assert again["comments"] == []  # nothing is commented twice
    assert "No new findings." in again["body"]
    assert "1 finding(s) from an earlier push are still open" in again["body"]
    run = run_of(db_session, "b" * 40)
    assert run.status == runs.OK
    db_session.expire_all()
    rows = list(db_session.scalars(select(FindingRow).where(FindingRow.run_id == run.id)))
    assert [(r.posted, r.drop_reason) for r in rows] == [(False, ALREADY_POSTED)]


@respx.mock
async def test_a_new_bug_on_the_second_push_is_still_commented(
    db_session: Session, enqueue: Enqueue, drain: Drain, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.jobs import handlers
    from app.review.fake import review_file

    first, second = two_pushes()
    gh = Pipeline(first)
    enqueue(first)
    await drain()

    async def old_and_new(path: str, numbered: str) -> list[Finding]:
        old = review_file(path, numbered)  # the password finding again
        todo = old[0].model_copy(update={"category": Category.UNSAFE_EXEC, "title": "new"})
        return [*old, todo]

    monkeypatch.setattr(handlers, "build_reviewers", lambda: old_and_new)
    gh.head.mock(return_value=httpx.Response(200, json={"head": {"sha": second.head_sha}}))
    enqueue(second)
    await drain()

    sent = gh.sent_review(1)
    assert [c["body"].count("new") > 0 for c in sent["comments"]] == [True]  # only the new one
    assert "1 finding(s) from an earlier push are still open" in sent["body"]


@respx.mock
async def test_findings_of_a_review_that_never_went_out_are_not_treated_as_known(
    db_session: Session, enqueue: Enqueue, drain: Drain
) -> None:
    """A superseded run saves its findings but posts nothing: they were never shown."""
    first, second = two_pushes()
    gh = Pipeline(first, head_sha="f" * 40)  # GitHub's head is already someone else's commit
    enqueue(first)
    await drain()
    assert gh.reviews.call_count == 0 and run_of(db_session, "a" * 40).status == runs.SUPERSEDED

    gh.head.mock(return_value=httpx.Response(200, json={"head": {"sha": second.head_sha}}))
    enqueue(second)
    await drain()
    assert [c["path"] for c in gh.sent_review(0)["comments"]] == ["app/db.py"]
