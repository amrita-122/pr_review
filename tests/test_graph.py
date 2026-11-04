import structlog.testing

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

    with structlog.testing.capture_logs() as logs:
        outcome = await review_diff(_files("good.py", "bad.py"), reviewer, LIMITS)
    assert [f.file_path for f in outcome.findings] == ["good.py"]
    assert set(outcome.reviewed) == {"good.py"}
    assert outcome.skipped == [("bad.py", "security review failed")]
    failed = [e for e in logs if e["event"] == "file_review_failed"]
    assert failed[0]["error"] == "RuntimeError" and failed[0]["detail"] == "model exploded"
    assert "model exploded" not in outcome.skipped[0][1]  # the PR never sees provider text


async def test_invalid_line_is_dropped_by_the_validator() -> None:
    async def reviewer(path: str, numbered: str) -> list[Finding]:
        return [_finding(path, line=2), _finding(path, line=99)]

    with structlog.testing.capture_logs() as logs:
        outcome = await review_diff(_files("a.py"), reviewer, LIMITS)
    assert [f.line for f in outcome.findings] == [2]
    assert outcome.raw_count == 2
    assert [(f.line, reason) for f, reason in outcome.dropped] == [(99, "line not commentable")]
    assert [e["reason"] for e in logs if e["event"] == "finding_dropped"] == [
        "line not commentable"
    ]


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


async def test_total_size_limit_skips_files_that_do_not_fit() -> None:
    called: list[str] = []

    async def reviewer(path: str, numbered: str) -> list[Finding]:
        called.append(path)
        return []

    files = _files("a.py", "b.py", "c.py")
    size = len(files["a.py"].numbered)
    limits = ReviewLimits(max_files=10, max_chars_per_file=1000, max_total_chars=size * 2)
    outcome = await review_diff(files, reviewer, limits)
    assert called == ["a.py", "b.py"]
    assert outcome.skipped == [("c.py", "over the total PR size limit")]


async def test_findings_per_file_are_capped_keeping_the_most_severe() -> None:
    def make(line: int, severity: Severity) -> Finding:
        # a different category per finding, so the aggregator does not merge them
        category = [Category.INJECTION, Category.SECRETS, Category.AUTHZ][line - 1]
        return _finding("a.py", line).model_copy(
            update={"severity": severity, "category": category}
        )

    async def reviewer(path: str, numbered: str) -> list[Finding]:
        return [make(1, Severity.LOW), make(2, Severity.CRITICAL), make(3, Severity.MEDIUM)]

    limits = ReviewLimits(max_files=10, max_chars_per_file=1000, max_findings_per_file=2)
    outcome = await review_diff(_files("a.py"), reviewer, limits)
    assert outcome.raw_count == 2
    assert {f.severity for f in outcome.findings} == {Severity.CRITICAL, Severity.MEDIUM}


async def test_comment_cap_keeps_most_severe_and_records_the_drop() -> None:
    patch = "@@ -0,0 +1,4 @@\n+a\n+b\n+c\n+d"
    files = {"a.py": parse_patch("a.py", patch)}

    async def reviewer(path: str, numbered: str) -> list[Finding]:
        sev = [Severity.LOW, Severity.HIGH, Severity.CRITICAL, Severity.MEDIUM]
        cats = [Category.INJECTION, Category.SECRETS, Category.AUTHZ, Category.CRYPTO]
        return [
            _finding(path, i + 1).model_copy(update={"severity": s, "category": cats[i]})
            for i, s in enumerate(sev)
        ]

    limits = ReviewLimits(max_files=10, max_chars_per_file=1000, max_comments=2)
    outcome = await review_diff(files, reviewer, limits)
    assert {f.severity for f in outcome.findings} == {Severity.CRITICAL, Severity.HIGH}
    assert sorted(reason for _, reason in outcome.dropped) == ["over comment cap"] * 2
