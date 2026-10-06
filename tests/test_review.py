import logging

import pytest
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


def test_finding_rejects_bad_enum_and_line() -> None:
    with pytest.raises(ValidationError):
        ReviewResult.model_validate(
            {"findings": [{**_finding().model_dump(), "severity": "catastrophic"}]}
        )
    with pytest.raises(ValidationError):
        _finding(line=0)


def test_fake_reviewer_finds_todo_and_password() -> None:
    parsed = parse_patch("a.py", PATCH)
    findings = review_file("a.py", parsed.numbered)
    assert [(f.line, f.category) for f in findings] == [(2, Category.OTHER), (3, Category.SECRETS)]


def test_fake_reviewer_ignores_context_and_removed_lines() -> None:
    parsed = parse_patch("a.py", "@@ -1,2 +1,1 @@\n # TODO old\n-password = 'x'")
    assert review_file("a.py", parsed.numbered) == []


def test_validator_drops_out_of_diff_line_and_unreviewed_file(
    caplog: pytest.LogCaptureFixture,
) -> None:
    reviewed = {"a.py": parse_patch("a.py", PATCH)}
    good, bad_line, bad_file = _finding(line=2), _finding(line=99), _finding(path="b.py")
    with caplog.at_level(logging.WARNING):
        kept = validate_findings([good, bad_line, bad_file], reviewed)
    assert kept == [good]
    assert "line not commentable" in caplog.text
    assert "file not reviewed" in caplog.text
