import logging

import pytest

from app.diff.parse import ParsedFile, parse_patch
from app.review.graph import ReviewLimits, review_diff
from app.review.schemas import Category, Finding, Severity

LIMITS = ReviewLimits(max_files=10, max_chars_per_file=1000)
PATCH = "@@ -1,2 +1,3 @@\n import os\n+query = f'select {x}'\n print(1)"


def _finding(path: str, line: int = 2) -> Finding:
    return Finding(
        file_path=path,
        line=line,
        severity=Severity.HIGH,
        category=Category.INJECTION,
        title="t",
        rationale="r",
    )


def _files(*paths: str) -> dict[str, ParsedFile]:
    return {p: parse_patch(p, PATCH) for p in paths}


async def test_findings_from_all_files_are_collected() -> None:
    async def reviewer(path: str, numbered: str) -> list[Finding]:
        return [_finding(path)]

    outcome = await review_diff(_files("a.py", "b.py"), reviewer, LIMITS)
    assert sorted(f.file_path for f in outcome.findings) == ["a.py", "b.py"]
    assert set(outcome.reviewed) == {"a.py", "b.py"}
    assert outcome.skipped == []


async def test_reviewer_receives_the_numbered_diff() -> None:
    seen: list[str] = []

    async def reviewer(path: str, numbered: str) -> list[Finding]:
        seen.append(numbered)
        return []

    await review_diff(_files("a.py"), reviewer, LIMITS)
    assert "L2 + query" in seen[0]


async def test_one_failing_file_does_not_fail_the_run() -> None:
    async def reviewer(path: str, numbered: str) -> list[Finding]:
        if path == "bad.py":
            raise RuntimeError("model exploded")
        return [_finding(path)]

    outcome = await review_diff(_files("good.py", "bad.py"), reviewer, LIMITS)
    assert [f.file_path for f in outcome.findings] == ["good.py"]
    assert set(outcome.reviewed) == {"good.py"}
    assert outcome.skipped == [("bad.py", "review failed")]


async def test_invalid_line_is_dropped_by_the_validator(
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def reviewer(path: str, numbered: str) -> list[Finding]:
        return [_finding(path, line=2), _finding(path, line=99)]

    with caplog.at_level(logging.WARNING):
        outcome = await review_diff(_files("a.py"), reviewer, LIMITS)
    assert [f.line for f in outcome.findings] == [2]
    assert "line not commentable" in caplog.text


async def test_file_and_size_limits_skip_with_reason() -> None:
    called: list[str] = []

    async def reviewer(path: str, numbered: str) -> list[Finding]:
        called.append(path)
        return []

    files = _files("a.py", "b.py", "c.py")
    outcome = await review_diff(files, reviewer, ReviewLimits(max_files=2, max_chars_per_file=1000))
    assert called == ["a.py", "b.py"]
    assert outcome.skipped == [("c.py", "over the 2-file limit")]

    called.clear()
    outcome = await review_diff(files, reviewer, ReviewLimits(max_files=10, max_chars_per_file=5))
    assert called == []
    assert len(outcome.skipped) == 3
    assert all("too large" in reason for _, reason in outcome.skipped)


async def test_no_files_means_no_model_calls() -> None:
    async def reviewer(path: str, numbered: str) -> list[Finding]:
        raise AssertionError("must not be called")

    outcome = await review_diff({}, reviewer, LIMITS)
    assert outcome.findings == [] and outcome.reviewed == {}
