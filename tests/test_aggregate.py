import pytest

from app.review.aggregate import MERGED, OVER_CAP, aggregate
from app.review.render import render_summary
from app.review.schemas import Agent, Category, Finding, ReviewFinding, Severity


def _f(
    line: int,
    category: Category = Category.INJECTION,
    severity: Severity = Severity.HIGH,
    agent: Agent = Agent.SECURITY,
    file: str = "a.py",
) -> ReviewFinding:
    return ReviewFinding(
        file_path=file,
        line=line,
        severity=severity,
        category=category,
        title="t",
        rationale="r",
        agent=agent,
    )


def test_same_category_within_two_lines_is_one_problem_keeping_the_most_severe() -> None:
    low, high = _f(10, severity=Severity.LOW), _f(12, severity=Severity.CRITICAL)
    kept, dropped = aggregate([low, high], max_comments=10)
    assert kept == [high] and dropped == [(low, MERGED)]


def test_three_lines_apart_is_two_problems() -> None:
    kept, dropped = aggregate([_f(10), _f(13)], max_comments=10)
    assert len(kept) == 2 and dropped == []


def test_different_categories_on_nearby_lines_are_kept_apart() -> None:
    kept, _ = aggregate([_f(10), _f(11, Category.SECRETS)], max_comments=10)
    assert len(kept) == 2


def test_two_agents_on_the_exact_same_line_merge_and_security_wins_a_tie() -> None:
    security = _f(7, Category.INJECTION, Severity.HIGH, Agent.SECURITY)
    quality = _f(7, Category.BUG, Severity.HIGH, Agent.QUALITY)
    kept, dropped = aggregate([quality, security], max_comments=10)
    assert kept == [security] and dropped == [(quality, MERGED)]


def test_a_more_severe_quality_finding_beats_a_security_one_on_the_same_line() -> None:
    security = _f(7, severity=Severity.LOW)
    quality = _f(7, Category.BUG, Severity.CRITICAL, Agent.QUALITY)
    kept, _ = aggregate([security, quality], max_comments=10)
    assert kept == [quality]


def test_two_agents_on_neighbouring_lines_both_stay() -> None:
    kept, _ = aggregate([_f(7), _f(8, Category.BUG, agent=Agent.QUALITY)], max_comments=10)
    assert len(kept) == 2


def test_different_files_never_merge() -> None:
    kept, _ = aggregate([_f(5, file="a.py"), _f(5, file="b.py")], max_comments=10)
    assert len(kept) == 2


def test_cap_keeps_the_most_severe_sorted_and_reports_the_rest() -> None:
    findings = [
        _f(1, severity=Severity.LOW),
        _f(20, severity=Severity.CRITICAL),
        _f(40, severity=Severity.MEDIUM),
        _f(60, severity=Severity.HIGH),
    ]
    kept, dropped = aggregate(findings, max_comments=2)
    assert [f.severity for f in kept] == [Severity.CRITICAL, Severity.HIGH]
    assert {f.severity for f, _ in dropped} == {Severity.MEDIUM, Severity.LOW}
    assert {reason for _, reason in dropped} == {OVER_CAP}


def test_output_is_ordered_by_severity_then_agent_then_location() -> None:
    items = [
        _f(30, severity=Severity.HIGH, agent=Agent.QUALITY, category=Category.BUG),
        _f(50, severity=Severity.HIGH),
        _f(10, severity=Severity.HIGH, file="b.py"),
        _f(90, severity=Severity.CRITICAL, agent=Agent.QUALITY, category=Category.BUG),
    ]
    kept, _ = aggregate(items, max_comments=10)
    assert [(f.file_path, f.line) for f in kept] == [
        ("a.py", 90),
        ("a.py", 50),
        ("b.py", 10),
        ("a.py", 30),
    ]


def test_plain_findings_without_an_agent_count_as_security() -> None:
    plain = Finding(
        file_path="a.py",
        line=3,
        severity=Severity.HIGH,
        category=Category.INJECTION,
        title="t",
        rationale="r",
    )
    kept, _ = aggregate([plain, _f(3, Category.BUG, agent=Agent.QUALITY)], max_comments=10)
    assert kept == [plain]  # same line, different agents, equal severity: security wins


@pytest.mark.parametrize("n", [0, 1, 5])
def test_empty_and_small_inputs(n: int) -> None:
    findings = [_f(i * 10 + 1) for i in range(n)]
    kept, dropped = aggregate(findings, max_comments=25)
    assert len(kept) == n and dropped == []


def test_summary_groups_by_category_and_states_overflow_and_merges() -> None:
    findings = [
        _f(1),
        _f(40, Category.BUG, agent=Agent.QUALITY),
        _f(80, Category.BUG, agent=Agent.QUALITY),
    ]
    text = render_summary(3, findings, [], overflow=4, merged=2)
    assert "3 finding(s)" in text
    assert "By category: 2 bug, 1 injection" in text
    assert "4 lower-priority finding(s) were not posted" in text
    assert "2 duplicate finding(s) were merged" in text


def test_summary_stays_quiet_when_nothing_was_cut() -> None:
    text = render_summary(1, [_f(1)], [])
    assert "not posted" not in text and "merged" not in text and "By category" not in text
