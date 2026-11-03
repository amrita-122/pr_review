"""Matching and scoring. Pure functions: no model, no IO, fully unit-testable."""

import statistics
from collections import defaultdict
from dataclasses import dataclass, field
from decimal import Decimal

from app.review.schemas import Finding
from evals.dataset import Expected

LINE_TOLERANCE = 3  # a finding within +-3 lines of the expected range counts as a hit


@dataclass
class CaseResult:
    case_id: str
    expected: list[Expected]
    findings: list[Finding]  # validated findings (what would be posted)
    raw_count: int  # findings the model produced before validation
    dropped_count: int  # removed by the validator
    errored: bool
    latency_s: float
    cost_usd: Decimal
    tokens_in: int = 0
    tokens_out: int = 0
    # Findings the validator removed. Red-team scoring needs them: "did the MODEL emit attacker
    # text" is a different question from "would it have been POSTED".
    dropped_findings: list[Finding] = field(default_factory=list)


@dataclass
class MatchOutcome:
    true_positives: list[tuple[Expected, Finding]] = field(default_factory=list)
    missed: list[Expected] = field(default_factory=list)
    false_positives: list[Finding] = field(default_factory=list)
    duplicates: list[Finding] = field(default_factory=list)  # extra hits on an already-found bug
    wrong_category: list[Finding] = field(default_factory=list)  # right place, wrong category


def _near(finding: Finding, exp: Expected) -> bool:
    return (
        finding.file_path == exp.file
        and exp.line_start - LINE_TOLERANCE <= finding.line <= exp.line_end + LINE_TOLERANCE
    )


def match_case(expected: list[Expected], findings: list[Finding]) -> MatchOutcome:
    """Each expected bug matches at most one finding (file + category equal, line within range).

    Extra findings on a bug that was already matched are duplicates, not false positives.
    Unmatched findings near an expected bug but in another category are false positives that are
    also listed in wrong_category (a diagnostic for ambiguous labels).
    """
    out = MatchOutcome()
    used: set[int] = set()
    matched_exp: set[int] = set()
    for ei, exp in enumerate(expected):
        for fi, finding in enumerate(findings):
            if fi not in used and finding.category == exp.category and _near(finding, exp):
                out.true_positives.append((exp, finding))
                used.add(fi)
                matched_exp.add(ei)
                break
        else:
            out.missed.append(exp)
    for fi, finding in enumerate(findings):
        if fi in used:
            continue
        if any(
            ei in matched_exp and finding.category == exp.category and _near(finding, exp)
            for ei, exp in enumerate(expected)
        ):
            out.duplicates.append(finding)
        else:
            out.false_positives.append(finding)
            if any(_near(finding, exp) for exp in expected):
                out.wrong_category.append(finding)
    return out


def _ratio(num: int, den: int) -> float | None:
    return num / den if den else None


def percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = round((len(ordered) - 1) * pct / 100)
    return ordered[index]


def score_run(results: list[CaseResult]) -> dict[str, object]:
    """One full pass over the golden set -> a JSON-serialisable metrics dict."""
    tp = fn = fp = dup = wrong = 0
    per_cat: dict[str, dict[str, int]] = defaultdict(lambda: {"tp": 0, "fn": 0, "fp": 0})
    clean_cases = clean_with_fp = clean_fp = 0
    for r in results:
        m = match_case(r.expected, r.findings)
        tp += len(m.true_positives)
        fn += len(m.missed)
        fp += len(m.false_positives)
        dup += len(m.duplicates)
        wrong += len(m.wrong_category)
        for exp, _ in m.true_positives:
            per_cat[exp.category.value]["tp"] += 1
        for exp in m.missed:
            per_cat[exp.category.value]["fn"] += 1
        for finding in m.false_positives:
            per_cat[finding.category.value]["fp"] += 1
        if not r.expected:
            clean_cases += 1
            clean_fp += len(m.false_positives)
            clean_with_fp += bool(m.false_positives)
    raw = sum(r.raw_count for r in results)
    latencies = [r.latency_s for r in results]
    cost = sum((r.cost_usd for r in results), Decimal(0))
    return {
        "cases": len(results),
        "errors": sum(r.errored for r in results),
        "precision": _ratio(tp, tp + fp),
        "recall": _ratio(tp, tp + fn),
        "true_positives": tp,
        "false_negatives": fn,
        "false_positives": fp,
        "duplicates": dup,
        "wrong_category": wrong,
        "per_category": {
            cat: {
                **c,
                "precision": _ratio(c["tp"], c["tp"] + c["fp"]),
                "recall": _ratio(c["tp"], c["tp"] + c["fn"]),
            }
            for cat, c in sorted(per_cat.items())
        },
        "clean_cases": clean_cases,
        "clean_cases_with_fp": clean_with_fp,
        "clean_false_positives": clean_fp,
        "invalid_line_rate": _ratio(sum(r.dropped_count for r in results), raw),
        "cost_usd_total": str(cost),
        "cost_usd_per_case": str(cost / len(results)) if results else "0",
        "tokens_in": sum(r.tokens_in for r in results),
        "tokens_out": sum(r.tokens_out for r in results),
        "latency_p50_s": percentile(latencies, 50),
        "latency_p95_s": percentile(latencies, 95),
    }


def summarize(runs: list[dict[str, object]], key: str) -> dict[str, float | None]:
    """mean / min / max of a numeric metric across runs (None entries are ignored)."""
    vals = [float(v) for r in runs if isinstance(v := r.get(key), int | float)]
    if not vals:
        return {"mean": None, "min": None, "max": None}
    return {"mean": statistics.fmean(vals), "min": min(vals), "max": max(vals)}
