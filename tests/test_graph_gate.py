"""The human-confirmation gate: pause on held findings, resume once, never start over."""

import pytest
from langgraph.checkpoint.memory import InMemorySaver

from app.diff.parse import ParsedFile, parse_patch
from app.review.graph import ReviewLimits, resume_review, review_diff
from app.review.schemas import Category, Finding, Severity

LIMITS = ReviewLimits(max_files=10, max_chars_per_file=1000)
HOLD = frozenset({Severity.CRITICAL})
PATCH = "@@ -1,2 +1,3 @@\n import os\n+query = f'select {x}'\n print(1)"
THREAD = "run-1"


def finding(path: str, severity: Severity) -> Finding:
    return Finding(
        file_path=path,
        line=2,
        severity=severity,
        category=Category.INJECTION,
        title="t",
        rationale="r",
    )


def files(*paths: str) -> dict[str, ParsedFile]:
    return {p: parse_patch(p, PATCH) for p in paths}


class Counting:
    """A fake reviewer that counts its model calls."""

    def __init__(self, severity: Severity) -> None:
        self.severity, self.calls = severity, 0

    async def __call__(self, path: str, numbered: str) -> list[Finding]:
        self.calls += 1
        return [finding(path, self.severity)]


async def must_not_be_called(path: str, numbered: str) -> list[Finding]:
    raise AssertionError("the model must not run on resume")


async def run(reviewer: Counting, saver: InMemorySaver | None, **extra: object):  # noqa: ANN201
    return await review_diff(
        files("a.py"),
        reviewer,
        LIMITS,
        hold_severities=HOLD,
        checkpointer=saver,
        thread_id=THREAD if saver else None,
        **extra,  # type: ignore[arg-type]
    )


async def test_critical_finding_pauses_and_carries_no_findings() -> None:
    outcome = await run(Counting(Severity.CRITICAL), InMemorySaver())
    assert outcome.pending == 1
    assert outcome.findings == []  # nothing a caller could post


async def test_non_held_severity_finishes_normally() -> None:
    outcome = await run(Counting(Severity.HIGH), InMemorySaver())
    assert outcome.pending == 0
    assert [f.file_path for f in outcome.findings] == ["a.py"]


async def test_without_a_checkpointer_nothing_is_held() -> None:
    outcome = await run(Counting(Severity.CRITICAL), None)
    assert outcome.pending == 0 and len(outcome.findings) == 1  # evals / flag off: as before


async def test_resume_returns_the_held_findings_without_calling_the_model_again() -> None:
    saver = InMemorySaver()
    await run(Counting(Severity.CRITICAL), saver)
    outcome = await resume_review(THREAD, must_not_be_called, LIMITS, HOLD, saver, approved=True)
    assert outcome.pending == 0
    assert [(f.file_path, f.severity) for f in outcome.findings] == [("a.py", Severity.CRITICAL)]
    assert set(outcome.reviewed) == {"a.py"}


async def test_not_approved_yields_no_findings() -> None:
    saver = InMemorySaver()
    await run(Counting(Severity.CRITICAL), saver)
    outcome = await resume_review(THREAD, must_not_be_called, LIMITS, HOLD, saver, approved=False)
    assert outcome.findings == [] and outcome.pending == 0


async def test_a_retry_after_the_pause_does_not_review_again() -> None:
    reviewer, saver = Counting(Severity.CRITICAL), InMemorySaver()
    first = await run(reviewer, saver)
    again = await run(reviewer, saver)
    assert reviewer.calls == 1  # the paused thread is reused
    assert (first.pending, again.pending) == (1, 1)


async def test_a_finished_thread_is_cleared_so_findings_are_not_duplicated() -> None:
    reviewer, saver = Counting(Severity.HIGH), InMemorySaver()
    await run(reviewer, saver)
    second = await run(reviewer, saver)
    assert reviewer.calls == 2
    assert len(second.findings) == 1  # not 2: the append reducer did not see the old run


async def test_resume_without_a_paused_thread_is_an_error() -> None:
    with pytest.raises(ValueError, match="no paused review"):
        await resume_review(
            THREAD, Counting(Severity.CRITICAL), LIMITS, HOLD, InMemorySaver(), approved=True
        )


async def test_a_checkpointer_needs_a_thread_id() -> None:
    with pytest.raises(ValueError, match="thread_id"):
        await review_diff(
            files("a.py"), Counting(Severity.HIGH), LIMITS, hold_severities=HOLD,
            checkpointer=InMemorySaver(),
        )  # fmt: skip


async def test_pre_skipped_files_appear_once_and_survive_a_resume() -> None:
    reviewer, saver = Counting(Severity.CRITICAL), InMemorySaver()
    paused = await run(reviewer, saver, pre_skipped=[("uv.lock", "lockfile")])
    resumed = await resume_review(THREAD, reviewer, LIMITS, HOLD, saver, approved=True)
    assert paused.skipped == [("uv.lock", "lockfile")]
    assert resumed.skipped == [("uv.lock", "lockfile")]
