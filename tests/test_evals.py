from decimal import Decimal

import pytest

from app.review.schemas import Category, Finding, Severity
from evals.dataset import Expected, load_cases
from evals.metrics import CaseResult, match_case, score_run
from evals.run import Budget, CostCapExceeded, check_thresholds

PATH = "a.py"


def _exp(line: int = 10, category: Category = Category.INJECTION, file: str = PATH) -> Expected:
    return Expected(case_id="c", file=file, line_start=line, line_end=line, category=category)


def _find(line: int, category: Category = Category.INJECTION, file: str = PATH) -> Finding:
    return Finding(
        file_path=file,
        line=line,
        severity=Severity.HIGH,
        category=category,
        title="t",
        rationale="r",
    )


def _result(expected: list[Expected], findings: list[Finding], **kw: object) -> CaseResult:
    base: dict[str, object] = {
        "case_id": "c",
        "raw_count": len(findings),
        "dropped_count": 0,
        "errored": False,
        "latency_s": 1.0,
        "cost_usd": Decimal("0.01"),
    }
    base.update(kw)
    return CaseResult(expected=expected, findings=findings, **base)  # type: ignore[arg-type]


def test_line_within_tolerance_matches_and_outside_does_not() -> None:
    assert len(match_case([_exp(10)], [_find(13)]).true_positives) == 1
    out = match_case([_exp(10)], [_find(14)])
    assert out.true_positives == [] and len(out.missed) == 1 and len(out.false_positives) == 1


def test_file_and_category_must_match() -> None:
    assert match_case([_exp()], [_find(10, file="b.py")]).true_positives == []
    wrong = match_case([_exp()], [_find(10, Category.SECRETS)])
    assert wrong.true_positives == [] and len(wrong.wrong_category) == 1


def test_one_finding_matches_one_bug_and_extras_are_duplicates() -> None:
    out = match_case([_exp(10)], [_find(10), _find(11)])
    assert len(out.true_positives) == 1
    assert len(out.duplicates) == 1 and out.false_positives == []
    two_bugs = match_case([_exp(10), _exp(11)], [_find(10)])
    assert len(two_bugs.true_positives) == 1 and len(two_bugs.missed) == 1


def test_score_run_precision_recall_and_clean_false_positives() -> None:
    results = [
        _result([_exp(10)], [_find(10)]),  # hit
        _result([_exp(5, Category.SECRETS)], []),  # miss
        _result([], [_find(1)]),  # clean case with a false positive
        _result([], []),  # clean and quiet
    ]
    m = score_run(results)
    assert (m["true_positives"], m["false_negatives"], m["false_positives"]) == (1, 1, 1)
    assert m["precision"] == 0.5 and m["recall"] == 0.5
    assert (m["clean_cases"], m["clean_cases_with_fp"]) == (2, 1)
    assert m["cost_usd_per_case"] == "0.01"


def test_invalid_line_rate_uses_raw_findings() -> None:
    m = score_run([_result([], [], raw_count=4, dropped_count=1)])
    assert m["invalid_line_rate"] == 0.25
    assert score_run([_result([], [])])["invalid_line_rate"] is None


def test_gate_fails_below_threshold_and_on_errors() -> None:
    summary = {
        "precision": {"mean": 0.7, "min": 0.6, "max": 0.8},
        "recall": {"mean": 0.9, "min": 0.9, "max": 0.9},
        "errors": {"mean": 0.0, "min": 0.0, "max": 1.0},
    }
    failures = check_thresholds(
        summary, {"min_precision": 0.75, "min_recall": 0.8, "max_errors": 0}
    )
    assert len(failures) == 2 and "precision" in failures[0] and "errored" in failures[1]
    assert check_thresholds(summary, {"min_precision": 0.5}) == []


def test_budget_cap_aborts() -> None:
    budget = Budget(Decimal("0.05"))
    budget.add(Decimal("0.04"))
    with pytest.raises(CostCapExceeded):
        budget.add(Decimal("0.02"))


def test_golden_set_is_well_formed() -> None:
    cases = load_cases()
    assert len(cases) >= 30
    assert sum(c.is_clean for c in cases) >= 10
    for case in cases:
        for exp in case.expected:
            assert exp.file == case.path, case.case_id
            # the expected line must be one the reviewer can actually point at
            lines = range(exp.line_start, exp.line_end + 1)
            assert all(n in case.parsed.commentable_lines for n in lines), case.case_id
            assert f"L{exp.line_start} +" in case.parsed.numbered, (
                f"{case.case_id}: not an added line"
            )


async def test_run_case_with_fake_reviewer_scores_a_perfect_hit() -> None:
    import asyncio

    from app.review.cost import Prices
    from app.review.graph import ReviewLimits
    from evals.run import run_case

    case = next(c for c in load_cases() if c.case_id == "inj_sql_fstring")
    exp = case.expected[0]

    async def oracle(path: str, numbered: str) -> list[Finding]:
        return [_find(exp.line_start, exp.category, file=path)]

    result = await run_case(
        case,
        oracle,
        ReviewLimits(10, 20000),
        Prices(Decimal(2), Decimal(10)),
        Budget(Decimal(1)),
        asyncio.Semaphore(1),
    )
    assert not result.errored
    assert len(match_case(case.expected, result.findings).true_positives) == 1


async def test_run_case_scores_only_the_bugs_the_running_agents_can_find() -> None:
    import asyncio

    from app.review.cost import Prices
    from app.review.graph import ReviewLimits
    from evals.run import run_case

    case = next(c for c in load_cases() if c.case_id == "inj_sql_fstring")
    quality_bug = Expected(
        case_id=case.case_id,
        file=case.path,
        line_start=1,
        line_end=1,
        category=Category.RESOURCE_LEAK,
    )
    mixed = case.model_copy(update={"expected": [*case.expected, quality_bug]})

    async def nothing(path: str, numbered: str) -> list[Finding]:
        return []

    args = (ReviewLimits(10, 20000), Prices(Decimal(2), Decimal(10)), Budget(Decimal(1)))
    security_only = await run_case(mixed, {"security": nothing}, *args, asyncio.Semaphore(1))
    both = await run_case(
        mixed, {"security": nothing, "quality": nothing}, *args, asyncio.Semaphore(1)
    )
    assert [e.category for e in security_only.expected] == [Category.INJECTION]
    assert {e.category for e in both.expected} == {Category.INJECTION, Category.RESOURCE_LEAK}


def _pr_dir(tmp_path, expected_rows: list[str]):  # type: ignore[no-untyped-def]
    (tmp_path / "cases").mkdir()
    pr = tmp_path / "prs" / "pr_demo"
    pr.mkdir(parents=True)
    for name, body in (("a", "x = 1"), ("b", "y = 2")):
        (pr / f"{name}.diff").write_text(
            f"diff --git a/{name}.py b/{name}.py\n--- /dev/null\n+++ b/{name}.py\n"
            f"@@ -0,0 +1,1 @@\n+{body}\n",
            encoding="utf-8",
        )
    (tmp_path / "expected.jsonl").write_text("\n".join(expected_rows) + "\n", encoding="utf-8")
    return tmp_path


def test_multi_file_pr_cases_load_with_their_labels(tmp_path) -> None:  # type: ignore[no-untyped-def]
    from evals.dataset import load_pr_cases

    row = (
        '{"case_id": "pr_demo", "file": "b.py", "line_start": 1, "line_end": 1,'
        ' "category": "injection"}'
    )
    (case,) = load_pr_cases(_pr_dir(tmp_path, [row]))
    assert case.case_id == "pr_demo" and set(case.files) == {"a.py", "b.py"}
    assert [e.file for e in case.expected] == ["b.py"]


def test_pr_case_label_for_a_file_that_is_not_in_the_pr_is_rejected(tmp_path) -> None:  # type: ignore[no-untyped-def]
    from evals.dataset import load_pr_cases

    row = (
        '{"case_id": "pr_demo", "file": "nope.py", "line_start": 1, "line_end": 1,'
        ' "category": "injection"}'
    )
    with pytest.raises(ValueError, match="unknown file"):
        load_pr_cases(_pr_dir(tmp_path, [row]))


def test_single_file_loader_ignores_pr_labels_and_there_are_no_pr_cases_yet() -> None:
    from evals.dataset import load_pr_cases

    assert load_pr_cases() == []  # you add evals/data/prs/<id>/*.diff by hand


def test_score_run_reports_precision_and_recall_per_agent() -> None:
    results = [
        _result([_exp(10, Category.INJECTION), _exp(5, Category.BUG)], [_find(10), _find(99)]),
    ]
    m = score_run(results)
    per_agent = m["per_agent"]
    assert per_agent["security"]["tp"] == 1 and per_agent["security"]["fp"] == 1  # type: ignore[index]
    assert per_agent["quality"]["fn"] == 1 and per_agent["quality"]["recall"] == 0.0  # type: ignore[index]


def test_score_run_counts_files_routed_to_the_cheap_model() -> None:
    m = score_run([_result([], [], files_total=4, files_low=3), _result([], [], files_total=2)])
    assert (m["files_total"], m["files_low"]) == (6, 3)
