"""Static checks on the golden set. No model, no network: run in CI's normal test job.

`uv run python -m evals.lint_dataset` prints problems (exit 1) and a balance summary.
"""

import sys
from collections import Counter
from pathlib import Path

from evals.dataset import DATA_DIR, Case, PRCase, load_cases, load_pr_cases


def _check_labels(case: Case | PRCase, files: dict[str, frozenset[int]]) -> list[str]:
    problems: list[str] = []
    for exp in case.expected:
        lines = files.get(exp.file)
        if lines is None:
            problems.append(f"{case.case_id}: label for unknown file {exp.file}")
        elif not any(n in lines for n in range(exp.line_start, exp.line_end + 1)):
            problems.append(
                f"{case.case_id}: lines {exp.line_start}-{exp.line_end} of {exp.file} "
                "are not in the diff (the validator would drop them)"
            )
    return problems


def lint(data_dir: Path = DATA_DIR) -> tuple[list[str], dict[str, int]]:
    cases = load_cases(data_dir)
    prs = load_pr_cases(data_dir)
    problems: list[str] = []

    ids = [c.case_id for c in cases] + [p.case_id for p in prs]
    problems += [f"duplicate case_id {i}" for i, n in Counter(ids).items() if n > 1]

    for case in cases:
        problems += _check_labels(case, {case.path: case.parsed.commentable_lines})
    for pr in prs:
        if len(pr.files) < 2:
            problems.append(f"{pr.case_id}: a PR case needs at least 2 files")
        problems += _check_labels(pr, {p: f.commentable_lines for p, f in pr.files.items()})

    stats = {
        "single_cases": len(cases),
        "single_clean": sum(c.is_clean for c in cases),
        "pr_cases": len(prs),
        "pr_clean": sum(p.is_clean for p in prs),
        "labels": sum(len(c.expected) for c in [*cases, *prs]),
    }
    for name, group in (("single", cases), ("pr", prs)):
        cats = Counter(e.category.value for c in group for e in c.expected)
        stats.update({f"{name}_label_{k}": v for k, v in sorted(cats.items())})
    return problems, stats


def main() -> int:
    problems, stats = lint()
    for key, value in stats.items():
        print(f"{key}: {value}")
    for problem in problems:
        print(f"PROBLEM: {problem}", file=sys.stderr)
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
