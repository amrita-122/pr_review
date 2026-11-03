"""Red-team side of the eval runner: `uv run python -m evals.run --suite redteam --label baseline`.

Runs every case twice (injected + control) through the same review_diff the app uses, scores
suppression / hijack (see evals/redteam.py), prints a table and saves
evals/results/<date>_redteam_<label>.json. Paid: it calls the real model.
"""

import argparse
import asyncio
import json
import re
import sys
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from app.config import Settings
from app.review.cost import Prices
from app.review.graph import ReviewLimits
from app.review.llm import Reviewer, build_default_reviewer
from evals.metrics import summarize
from evals.redteam import (
    RedTeamCase,
    RedTeamResult,
    aggregate_per_case,
    load_redteam_cases,
    score_redteam,
)
from evals.run import Budget, CostCapExceeded, run_case

LABEL = re.compile(r"[a-z0-9_]+")


def _pct(value: float | None) -> str:
    return "n/a" if value is None else f"{value * 100:5.1f}%"


async def run_redteam_once(
    cases: list[RedTeamCase], reviewer: Reviewer, settings: Settings, budget: Budget
) -> list[RedTeamResult]:
    from evals.run import CASE_CONCURRENCY

    limits = ReviewLimits.from_settings(settings)
    prices = Prices(settings.price_input_per_mtok, settings.price_output_per_mtok)
    sem = asyncio.Semaphore(CASE_CONCURRENCY)

    async def one(case: RedTeamCase) -> RedTeamResult:
        injected, control = await asyncio.gather(
            run_case(case.injected, reviewer, limits, prices, budget, sem),
            run_case(case.control, reviewer, limits, prices, budget, sem),
        )
        return RedTeamResult(case.row, injected, control)

    return list(await asyncio.gather(*(one(c) for c in cases)))


def print_redteam_report(label: str, model: str, runs: list[dict[str, Any]]) -> None:
    print(f"\nred-team label={label} model={model} runs={len(runs)}")
    print(
        f"{'run':>3} {'suppressed':>11} {'hijack(model)':>14} {'hijack(posted)':>15} "
        f"{'viol.':>6} {'errors':>6}"
    )
    for i, r in enumerate(runs, 1):
        sup = f"{r['suppressed']}/{r['suppression_denominator']}"
        hm = f"{r['hijack_model']}/{r['hijack_denominator']}"
        hp = f"{r['hijack_posted']}/{r['hijack_denominator']}"
        print(f"{i:>3} {sup:>11} {hm:>14} {hp:>15} {r['violations_posted']:>6} {r['errors']:>6}")
    n = len(runs)
    print(f"\nper case (how many of {n} runs):")
    print(
        f"  {'case':<28} {'ctl found':>9} {'inj found':>9} {'SUPPRESSED':>10} "
        f"{'hij.model':>9} {'hij.post':>8} {'err':>4}"
    )
    for c in aggregate_per_case(runs):
        print(
            f"  {c['case_id']:<28} {c['control_found']:>9} {c['injected_found']:>9} "
            f"{c['suppressed']:>10} {c['hijack_model']:>9} {c['hijack_posted']:>8} "
            f"{c['errored']:>4}"
        )


async def amain_redteam(args: argparse.Namespace, settings: Settings) -> int:
    from evals.run import _result_path

    prompt_version = args.prompt_version or settings.prompt_version
    label = args.label or prompt_version
    if not LABEL.fullmatch(label):  # the label becomes part of a file name
        print(f"invalid --label {label!r}: use [a-z0-9_]+", file=sys.stderr)
        return 2
    cases = load_redteam_cases()
    if args.case:
        unknown = set(args.case) - {c.row.case_id for c in cases}
        if unknown:
            print(f"unknown case ids: {sorted(unknown)}", file=sys.stderr)
            return 2
        cases = [c for c in cases if c.row.case_id in set(args.case)]
    reviewer = build_default_reviewer(settings, prompt_version)
    budget = Budget(Decimal(str(args.max_cost)))

    runs: list[dict[str, Any]] = []
    try:
        for _ in range(args.runs):
            runs.append(score_redteam(await run_redteam_once(cases, reviewer, settings, budget)))
    except CostCapExceeded as exc:
        print(f"aborted: {exc}", file=sys.stderr)
        return 2

    print_redteam_report(label, settings.model_review, runs)
    summary = {
        m: summarize(runs, m)
        for m in ("suppression_rate", "hijack_model_rate", "hijack_posted_rate", "errors")
    }
    for metric in ("suppression_rate", "hijack_model_rate", "hijack_posted_rate"):
        s = summary[metric]
        print(f"{metric}: mean {_pct(s['mean'])} (min {_pct(s['min'])}, max {_pct(s['max'])})")
    print(f"estimated total spend: ${budget.spent:.4f}")

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    path = _result_path(f"redteam_{label}", out_dir)
    path.write_text(
        json.dumps(
            {
                "suite": "redteam",
                "label": label,
                "prompt_version": prompt_version,
                "model": settings.model_review,
                "date": datetime.now(UTC).isoformat(timespec="seconds"),
                "cases": len(cases),
                "summary": summary,
                "per_case": aggregate_per_case(runs),
                "runs": [{k: v for k, v in r.items() if k != "per_case"} for r in runs],
                "total_cost_usd": str(budget.spent),
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    print(f"saved {path}")
    return 0
