"""Eval runner: `uv run python -m evals.run [--runs 3] [--prompt-version security_v1] [--check]`.

Calls review_diff directly (no GitHub, no database) on the golden set in evals/data and reports
precision / recall / cost / latency. This is the only place besides production that calls a real
model; unit tests never do. LangSmith tracing is NOT enabled here unless you export it yourself.
"""

import argparse
import asyncio
import json
import sys
import time
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from langchain_core.callbacks import UsageMetadataCallbackHandler

from app.config import Settings, get_settings
from app.logging import configure_logging
from app.review.cost import Prices, estimate_cost, usage_from_metadata
from app.review.graph import ReviewLimits, review_diff
from app.review.llm import Reviewer, build_default_reviewer
from evals.dataset import Case, load_cases
from evals.metrics import CaseResult, match_case, score_run, summarize

RESULTS_DIR = Path(__file__).parent / "results"
THRESHOLDS_FILE = Path(__file__).parent / "thresholds.json"
CASE_CONCURRENCY = 4  # parallel cases; each case already fans out over its files
DEFAULT_MAX_COST_USD = Decimal("3")


class CostCapExceeded(Exception):  # noqa: N818
    """The estimated spend passed --max-cost; the run is aborted."""


class Budget:
    def __init__(self, cap: Decimal) -> None:
        self.cap = cap
        self.spent = Decimal(0)

    def add(self, amount: Decimal) -> None:
        self.spent += amount
        if self.spent > self.cap:
            raise CostCapExceeded(f"estimated spend ${self.spent:.4f} passed cap ${self.cap}")


async def run_case(
    case: Case,
    reviewer: Reviewer,
    limits: ReviewLimits,
    prices: Prices,
    budget: Budget,
    sem: asyncio.Semaphore,
) -> CaseResult:
    usage_handler = UsageMetadataCallbackHandler()
    async with sem:
        started = time.perf_counter()
        outcome = await review_diff(
            {case.path: case.parsed},
            reviewer,
            limits,
            run_name=f"eval {case.case_id}",
            callbacks=[usage_handler],
        )
        latency = time.perf_counter() - started
    usage = usage_from_metadata(usage_handler.usage_metadata)
    cost = estimate_cost(usage, prices)
    budget.add(cost)
    return CaseResult(
        case_id=case.case_id,
        expected=case.expected,
        findings=outcome.findings,
        raw_count=outcome.raw_count,
        dropped_count=len(outcome.dropped),
        errored=case.path not in outcome.reviewed,  # failed or skipped = counted as a failure
        latency_s=latency,
        cost_usd=cost,
        tokens_in=usage.tokens_in,
        tokens_out=usage.tokens_out,
    )


async def run_once(
    cases: list[Case], reviewer: Reviewer, settings: Settings, budget: Budget
) -> list[CaseResult]:
    limits = ReviewLimits(settings.max_files_per_pr, settings.max_chars_per_file)
    prices = Prices(settings.price_input_per_mtok, settings.price_output_per_mtok)
    sem = asyncio.Semaphore(CASE_CONCURRENCY)
    return list(
        await asyncio.gather(*(run_case(c, reviewer, limits, prices, budget, sem) for c in cases))
    )


def case_details(results: list[CaseResult]) -> list[dict[str, Any]]:
    """Per-case outcome so a prompt change can be debugged: what was found, missed, spurious."""
    details = []
    for r in results:
        m = match_case(r.expected, r.findings)
        details.append(
            {
                "case_id": r.case_id,
                "errored": r.errored,
                "missed": [e.category.value for e in m.missed],
                "false_positives": [
                    {"line": f.line, "category": f.category.value, "title": f.title}
                    for f in m.false_positives
                ],
                "wrong_category": len(m.wrong_category),
            }
        )
    return details


def check_thresholds(
    summary: dict[str, dict[str, float | None]], thresholds: dict[str, Any]
) -> list[str]:
    """Return a list of failure messages (empty = gate passes). Gates compare the MEAN over runs."""
    failures: list[str] = []
    for metric, key in (("precision", "min_precision"), ("recall", "min_recall")):
        mean = summary[metric]["mean"]
        floor = thresholds.get(key)
        if floor is not None and (mean is None or mean < floor):
            failures.append(f"{metric} mean {mean} is below the minimum {floor}")
    max_errors = thresholds.get("max_errors")
    worst_errors = summary["errors"]["max"]
    if max_errors is not None and (worst_errors or 0) > max_errors:
        failures.append(f"{worst_errors} case(s) errored in a run (max allowed {max_errors})")
    return failures


def _pct(value: float | None) -> str:
    return "n/a" if value is None else f"{value * 100:5.1f}%"


def print_report(
    prompt_version: str, model: str, runs: list[dict[str, Any]], summary: dict[str, Any]
) -> None:
    print(f"\nprompt={prompt_version} model={model} runs={len(runs)}")
    print(
        f"{'run':>3} {'precision':>9} {'recall':>8} {'clean FP':>9} {'invalid%':>9} "
        f"{'errors':>6} {'$/case':>8} {'p50 s':>6} {'p95 s':>6}"
    )
    for i, r in enumerate(runs, 1):
        clean_fp = f"{r['clean_cases_with_fp']}/{r['clean_cases']}"
        print(
            f"{i:>3} {_pct(r['precision']):>9} {_pct(r['recall']):>8} {clean_fp:>9} "
            f"{_pct(r['invalid_line_rate']):>9} {r['errors']:>6} "
            f"{float(r['cost_usd_per_case']):>8.4f} "
            f"{r['latency_p50_s']:>6.1f} {r['latency_p95_s']:>6.1f}"
        )
    for metric in ("precision", "recall"):
        s = summary[metric]
        print(f"{metric}: mean {_pct(s['mean'])} (min {_pct(s['min'])}, max {_pct(s['max'])})")
    print("\nper category (last run):")
    for cat, c in runs[-1]["per_category"].items():
        counts = f"tp={c['tp']} fp={c['fp']} fn={c['fn']}"
        print(
            f"  {cat:<12} precision {_pct(c['precision'])}  recall {_pct(c['recall'])}  ({counts})"
        )


def _result_path(prompt_version: str, out_dir: Path) -> Path:
    base = f"{datetime.now(UTC):%Y-%m-%d}_{prompt_version}"
    path = out_dir / f"{base}.json"
    n = 2
    while path.exists():  # never overwrite an earlier result of the same day
        path = out_dir / f"{base}_{n}.json"
        n += 1
    return path


async def amain(args: argparse.Namespace) -> int:
    settings = get_settings()
    prompt_version = args.prompt_version or settings.prompt_version
    cases = load_cases()
    if args.case:
        unknown = set(args.case) - {c.case_id for c in cases}
        if unknown:
            print(f"unknown case ids: {sorted(unknown)}", file=sys.stderr)
            return 2
        cases = [c for c in cases if c.case_id in set(args.case)]
    reviewer = build_default_reviewer(settings, prompt_version)
    budget = Budget(Decimal(str(args.max_cost)))

    runs: list[dict[str, Any]] = []
    details: list[list[dict[str, Any]]] = []
    try:
        for _ in range(args.runs):
            results = await run_once(cases, reviewer, settings, budget)
            runs.append(score_run(results))
            details.append(case_details(results))
    except CostCapExceeded as exc:
        print(f"aborted: {exc}", file=sys.stderr)
        return 2

    summary = {
        m: summarize(runs, m) for m in ("precision", "recall", "errors", "invalid_line_rate")
    }
    print_report(prompt_version, settings.model_review, runs, summary)
    print(f"estimated total spend: ${budget.spent:.4f}")

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    path = _result_path(prompt_version, out_dir)
    path.write_text(
        json.dumps(
            {
                "prompt_version": prompt_version,
                "model": settings.model_review,
                "date": datetime.now(UTC).isoformat(timespec="seconds"),
                "cases": len(cases),
                "summary": summary,
                "runs": runs,
                "details": details,
                "total_cost_usd": str(budget.spent),
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    print(f"saved {path}")

    if args.check:
        thresholds = json.loads(Path(args.thresholds).read_text(encoding="utf-8"))
        failures = check_thresholds(summary, thresholds)
        for failure in failures:
            print(f"GATE FAILED: {failure}", file=sys.stderr)
        if failures:
            return 1
        print("gate passed")
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the reviewer on the golden set.")
    parser.add_argument("--runs", type=int, default=1, help="repeat the whole set N times")
    parser.add_argument("--prompt-version", help="e.g. security_v1 (default: PROMPT_VERSION)")
    parser.add_argument("--case", action="append", help="only this case id (repeatable)")
    parser.add_argument(
        "--max-cost",
        type=float,
        default=float(DEFAULT_MAX_COST_USD),
        help="abort above this estimated USD",
    )
    parser.add_argument("--check", action="store_true", help="exit 1 if a threshold is missed")
    parser.add_argument("--thresholds", default=str(THRESHOLDS_FILE))
    parser.add_argument("--out-dir", default=str(RESULTS_DIR))
    args = parser.parse_args()
    if args.runs < 1:
        parser.error("--runs must be >= 1")
    configure_logging()
    sys.exit(asyncio.run(amain(args)))


if __name__ == "__main__":
    main()
