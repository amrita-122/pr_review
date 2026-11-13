# ADR 0012 — Quality prompt: keep quality_v2 (v3 tried and rejected)

Status: Accepted · Date: 2026-11-13

## Context
TASKS.md 10.35 said quality precision was 35%. Reading the per-case findings of the first holdout and golden runs showed
most "false positives" were real defects the labels did not cover (unclosed SQLite connections, ignored HTTP status,
`ast.literal_eval` raising `TypeError`). After relabelling (see `evals/holdout/RELABEL_LOG.md`), a fresh quality-only run
on the 49-case golden set (3 runs, $0.99) gave precision 69% (61-80%) and recall 90% (85-92%).

What still looked like a real gap: exception-path handling. A lock released only on the normal path
(`qa_leak_lock_not_released`, missed 3 of 3 runs) and a swallowed `except Exception: pass` (missed 2 of 3).

## Decision
Keep `quality_v2` as the default. Do not ship a `quality_v3`.

I wrote 6 fresh golden cases (3 exception-path bugs, 3 clean look-alikes: `try/finally`, `with lock`, `except ... raise`)
and measured, 5 runs each, quality agent only:

| Prompt | Bug cases found | Clean false positives |
|---|---|---|
| quality_v2 | 14 of 15 | 0 |
| quality_v3 (v2 plus two short paragraphs: check the exception path between acquire and release; an `except` that only does `pass`) | 10 of 15 | 0 |

The extra wording made the model less likely to report, so it fails the acceptance rule (recall up, precision not down).

## Consequences
- The 6 new cases stay in `evals/data/` as a regression check for exception-path bugs.
- Rejected without trying (more cost or complexity for a gap that is now small): a severity floor, few-shot examples
  taken from dismissed findings, a second validation pass.
- The holdout set is contaminated for the quality agent on the exception-path cases (I read its findings), so do not
  quote holdout quality numbers as unbiased for those cases.
- Revisit if a larger sample (or real dismissed-finding data from production) shows the exception-path misses are
  frequent. n=15 per prompt is small; one miss moves recall by 7 points.
