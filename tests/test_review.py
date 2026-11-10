import pytest
import structlog.testing
from pydantic import ValidationError

from app.diff.parse import parse_patch
from app.review.fake import review_file
from app.review.schemas import Category, Finding, ReviewResult, Severity
from app.review.validate import validate_findings

PATCH = "@@ -1,2 +1,4 @@\n import os\n+# TODO fix\n+password = 'x'\n print(1)"


def _finding(path: str = "a.py", line: int = 2) -> Finding:
    return Finding(
        file_path=path,
        line=line,
        severity=Severity.LOW,
        category=Category.OTHER,
        title="t",
        rationale="r",
    )


def test_finding_itself_still_rejects_a_bad_line() -> None:
    with pytest.raises(ValidationError):
        _finding(line=0)


def test_one_malformed_finding_does_not_drop_the_files_other_findings() -> None:
    """10.9: a bad enum, a line of 0 or an empty title costs that finding only."""
    good = _finding().model_dump()
    result = ReviewResult.model_validate(
        {
            "findings": [
                {**good, "severity": "catastrophic"},
                {**good, "line": 0},
                {**good, "title": ""},
                "not even an object",
                {**good, "title": "kept"},
            ]
        }
    )
    assert [f.title for f in result.findings] == ["kept"]


def test_overlong_title_is_shortened_not_fatal() -> None:
    result = ReviewResult.model_validate(
        {"findings": [{**_finding().model_dump(), "title": "x" * 500}]}
    )
    assert len(result.findings) == 1 and len(result.findings[0].title) == 200


def test_a_findings_list_of_the_wrong_shape_is_still_an_error() -> None:
    with pytest.raises(ValidationError):
        ReviewResult.model_validate({"findings": "nope"})


def test_fake_reviewer_finds_todo_and_password() -> None:
    parsed = parse_patch("a.py", PATCH)
    findings = review_file("a.py", parsed.numbered)
    assert [(f.line, f.category) for f in findings] == [(2, Category.OTHER), (3, Category.SECRETS)]


def test_fake_reviewer_ignores_context_and_removed_lines() -> None:
    parsed = parse_patch("a.py", "@@ -1,2 +1,1 @@\n # TODO old\n-password = 'x'")
    assert review_file("a.py", parsed.numbered) == []


def test_validator_drops_out_of_diff_line_and_unreviewed_file() -> None:
    reviewed = {"a.py": parse_patch("a.py", PATCH)}
    good, bad_line, bad_file = _finding(line=2), _finding(line=99), _finding(path="b.py")
    with structlog.testing.capture_logs() as logs:
        kept = validate_findings([good, bad_line, bad_file], reviewed)
    assert kept == [good]
    assert {e["reason"] for e in logs} == {"line not commentable", "file not reviewed"}


def test_bound_run_id_appears_on_validator_logs() -> None:
    structlog.contextvars.clear_contextvars()
    structlog.contextvars.bind_contextvars(run_id="abc123")
    try:
        with structlog.testing.capture_logs(
            processors=[structlog.contextvars.merge_contextvars]
        ) as logs:
            validate_findings([_finding(line=99)], {"a.py": parse_patch("a.py", PATCH)})
    finally:
        structlog.contextvars.clear_contextvars()
    assert logs[0]["event"] == "finding_dropped"
    assert logs[0]["run_id"] == "abc123"
