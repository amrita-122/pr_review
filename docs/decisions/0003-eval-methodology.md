# ADR 0003 — How the reviewer is evaluated and gated

Status: Accepted · Date: 2026-11-02

## Context
Phase 3 shipped an LLM reviewer with no measurement. Prompt or model changes could silently make
it worse. The model API has no temperature control (see `fix: drop unsupported temperature
setting`), so outputs vary between runs.

## Decision
- **Golden set**: `evals/data/cases/*.diff` plus `evals/data/expected.jsonl`
  (`case_id, file, line_start, line_end, category`). 31 cases: 20 with one planted bug each, 11
  clean (including hard negatives: parameterized SQL, MD5 as a cache key, a placeholder password
  in a test). Expectations were written before any model run.
- **Matching**: a finding matches an expected bug when file and category are equal and its line is
  within the expected range +-3. Each expected bug matches at most one finding. Further findings on
  an already-matched bug are counted as duplicates, not false positives. Findings near a bug but in
  another category are false positives and also reported as `wrong_category` (shows label ambiguity).
- **Metrics**: precision and recall (overall and per category), false positives on clean cases,
  invalid-line rate (validator drops / raw findings), cost per case, p50/p95 latency. Cases that
  error count as failures, not as skipped.
- **Variance**: every number comes from 3 runs; the gate compares the mean.
- **Gate**: `evals/thresholds.json` (min precision, min recall, max errors). `.github/workflows/evals.yml`
  runs `python -m evals.run --runs 3 --check` only on PRs that touch `app/review/**`, `evals/**` or
  the workflow. Thresholds sit below the measured score of the shipped prompt to leave room for
  variance on a small set (one missed bug = 5 points of recall).
- **Cost control**: `--max-cost` aborts a run when the estimated spend passes the cap (default $3).
- **Security of the gate**: `pull_request` trigger only (never `pull_request_target`), read-only
  token, the API key is exposed only to the single eval step, fork PRs are skipped because they get
  no secrets, 15 minute timeout, superseded runs are cancelled.
- **Tests vs evals**: unit tests never call a real model (fakes and respx). Evals are the only
  place that does, and they are not traced to LangSmith unless the operator enables it.

## Consequences
+ A worse prompt fails CI with numbers, and the baseline is reproducible from the repo.
- 31 cases is small: a saturated score cannot show further improvement, and a prompt tuned against
  these cases is optimistic. Add cases when production misses or false alarms appear.
- Cost estimates use list prices and ignore cache discounts, so they are slight over-estimates.
- The eval run costs about $0.50 per 3 runs at current prices.
