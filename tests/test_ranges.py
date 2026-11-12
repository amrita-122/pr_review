"""Multi-line ranges and LEFT-side (deleted line) comments (10.33)."""

import pytest

from app.diff.parse import parse_patch
from app.jobs.handlers import to_review_comment
from app.review.render import render_comment
from app.review.schemas import Category, Finding, Severity
from app.review.validate import partition_findings

# old: 1 a / 2 check / 3 b / 4 c ; new: 1 a / 2 b / 3 NEW / 4 c  (line 2 deleted, "NEW" added)
PATCH = "@@ -1,4 +1,4 @@\n a\n-check\n b\n+NEW\n c\n"
# a second hunk far away, so a range cannot bridge the gap
GAP = PATCH + "@@ -20,1 +20,2 @@\n x\n+y\n"


def _f(line: int, **kw: object) -> Finding:
    return Finding(
        file_path="a.py",
        line=line,
        severity=Severity.HIGH,
        category=Category.AUTHZ,
        title="t",
        rationale="r",
        **kw,  # type: ignore[arg-type]
    )


def _check(finding: Finding, patch: str = PATCH) -> str | None:
    kept, dropped = partition_findings([finding], {"a.py": parse_patch("a.py", patch)})
    return dropped[0][1] if dropped else None if kept else "?"


def test_parse_records_deleted_old_line_numbers() -> None:
    parsed = parse_patch("a.py", PATCH)
    assert parsed.removed_lines == frozenset({2})
    assert parsed.commentable_lines == frozenset({1, 2, 3, 4})
    assert "-" in parsed.numbered  # the model still sees the deletion


def test_range_on_new_side_is_accepted() -> None:
    assert _check(_f(3, start_line=1)) is None


def test_left_side_comment_on_a_deleted_line_is_accepted() -> None:
    assert _check(_f(2, side="LEFT")) is None


def test_left_side_on_a_line_that_was_not_deleted_is_dropped() -> None:
    assert _check(_f(3, side="LEFT")) == "line not commentable"


def test_right_side_does_not_accept_a_deleted_line_number() -> None:
    assert _check(_f(2)) is None  # 2 is also a (context) line on the new side
    assert _check(_f(20), PATCH) == "line not commentable"


def test_range_may_not_span_a_gap_between_hunks() -> None:
    assert _check(_f(21, start_line=4), GAP) == "line not commentable"


@pytest.mark.parametrize("start", [5, 100])
def test_range_start_after_end_is_dropped(start: int) -> None:
    assert _check(_f(3, start_line=start)) == "range start after end"


def test_overlong_range_is_dropped() -> None:
    patch = "@@ -0,0 +1,30 @@\n" + "".join(f"+l{i}\n" for i in range(30))
    assert _check(_f(30, start_line=1), patch) == "range too long"
    assert _check(_f(20, start_line=1), patch) is None


def test_comment_payload_for_a_range_and_for_a_single_line() -> None:
    ranged = to_review_comment(_f(3, start_line=1)).model_dump(exclude_none=True)
    assert ranged["start_line"] == 1 and ranged["start_side"] == "RIGHT" and ranged["line"] == 3
    single = to_review_comment(_f(3)).model_dump(exclude_none=True)
    assert "start_line" not in single and single["side"] == "RIGHT"
    same = to_review_comment(_f(3, start_line=3)).model_dump(exclude_none=True)
    assert "start_line" not in same  # one-line "range" is a plain comment
    left = to_review_comment(_f(2, side="LEFT")).model_dump(exclude_none=True)
    assert left["side"] == "LEFT" and "start_line" not in left


def test_no_suggestion_block_on_a_deleted_line() -> None:
    assert "```suggestion" not in render_comment(_f(2, side="LEFT", replacement_code="x"))
    assert "```suggestion" in render_comment(_f(3, replacement_code="x"))


def test_bad_side_is_a_malformed_finding_not_a_crash() -> None:
    from app.review.schemas import ReviewResult

    raw = {"findings": [{**_f(3).model_dump(), "side": "MIDDLE"}, _f(3).model_dump()]}
    assert len(ReviewResult.model_validate(raw).findings) == 1
