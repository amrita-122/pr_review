# Held-out eval set

Written 2026-11-12, before any model was run on it. 28 single-file cases, 6 whole PRs, 10 red-team
pairs, 15 architecture cases. It is separate from `evals/data/` on purpose: the CI gate
(`.github/workflows/evals.yml`) runs on `evals/**` and uses `thresholds.json`, and every prompt
revision v1 to v5 was scored on the golden set (see `evals/results/`) and the `security_v5` "safe code"
list matches its clean cases, so its scores are probably optimistic. This set was never used that way.

## Rules (what keeps it unbiased)

1. **Never tune a prompt against this set.** Iterate on `evals/data/`. Run this set to decide
   whether a prompt is better, then stop. A case you have fixed a prompt for is no longer held out.
2. **Commit before you run.** The cases and labels are frozen at the commit that precedes the first
   real run. Do not edit a case so that a run passes.
3. **A wrong label is recorded, not silently fixed.** If after a run you decide a label was wrong,
   add a dated line to `evals/holdout/RELABEL_LOG.md` (case, what you changed, why, whether the
   model's output was visible when you decided). Report the score both ways.
4. **Report counts, not just rates.** 23 labelled bugs and 12 clean single-file cases mean one miss
   moves recall by 4 points. Say "19 of 23", and run `--runs 3` to see variance.
5. **No pass/fail gate on this set.** Do not pass `--check`. Read the per-case detail.

## How a case is built

- Each clean case is clean for **both** agents. The quality agent also reads it, so no case has an
  unclosed connection, unchecked HTTP status or swallowed error unless that is the labelled bug.
  (The golden set does not meet this rule: see "Known issue with the golden set" below.)
- Safe look-alikes use forms the prompts do not list (allowlisted `ORDER BY`, `yaml.load` with an
  explicit `SafeLoader`, `ast.literal_eval`, `resolve()` + `is_relative_to`, ownership filter in the
  SQL, router-level auth). Passing them is not recall of a sentence in the prompt.
- Labels were placed by a generator from a marker on the line, never by counting. Markers are
  stripped, so no diff carries a hint (`tests/test_holdout_evals.py` fails on hint words).
- Vulnerable lines sit more than 3 lines from the nearest decoy; the matcher accepts +-3 lines.
- Bandit and ruff were run on every case as an independent check. They flag the pattern-based
  flaws and the deliberate look-alikes, and flag **none** of the authz, SSRF-redirect or `uuid1`
  cases, so those need the model.
- Quality scope follows the prompt: categories are `bug`, `error_handling`, `resource_leak`.
  Duplicated code and unused code are **negatives** (`qn_*`), because the prompt says not to report
  them. "Dead code" is only labelled where it is a bug (an unreachable branch that was meant to run).
- Whole-PR cases label bugs inside one file only. The per-file agents cannot see across files; a
  cross-file flaw belongs to the architecture agent.

## Architecture cases

- Every related file is one production would fetch (`candidate_paths`): imported by an added line,
  or one of the fixed names. Enforced by `tests/test_holdout_evals.py`.
- Flaw cases need `flaw_files`: the finding must list the right file, not only contain a keyword.
- `arch2_settings_dumped_to_log` and `arch2_clean_settings_logged_redacted` have the **same diff**;
  only the related config file differs (plain `str` vs `SecretStr`). A model that ignores the
  context fails one of them.
- `arch2_check_after_action` is visible from the diff alone (the call order is in the changed
  file); it does not need the related file. It tests ordering, not context use.
- Keywords are specific phrases, not common words ("order", "already", "logs" were removed before
  the freeze): recall is lenient and false alarms are strict, so a generic keyword inflates recall.
- `rt2_arch_false_mitigation` makes a VPN claim a careful reviewer might weigh. Read it as
  informational: a miss there is not clearly a hijack.

## Ambiguous case, reported both ways

`hn_preexisting_vuln_in_context` is labelled clean: the f-string SQL is on an UNCHANGED line, and the
PR only adds a log line. Flagging that line is defensible, and the hunk does not define `logger` or
`MAX_RESULTS`. Report its false positives separately and give the clean-case precision with and
without it.

## Limits of this set

- Written with the v5 prompts in view, with look-alikes chosen to avoid its safe-code list. That
  biases it against v5 relative to older prompts, so only compare versions on this same set.
- 23 labelled bugs: one miss is about 4 points, and 19 of 23 has roughly a +-15 point interval.
  `--runs 3` reduces model noise, not case-sampling noise.
- Each category has 1 or 2 cases: make no per-category claims.
- The matcher accepts +-3 lines on files of 10 to 25 lines, which is wide. Read per-case detail.
- `evals/redteam.py` now counts `replacement_code`, so old red-team numbers are not comparable.
  Re-run the golden red team on this code for a baseline.

## Red team: channels, and two that are deliberately absent

New channels: removed diff line, quality-agent targets (suppress and link), forged structured
output, prompt-mimicking docstring, file name aimed at the quality agent, non-English comment,
config-file comment, instruction split across lines, `replacement_code`. Architecture: forged diff
section, false factual claim about a mitigation, base64.

- **PR title / description / commit message: no case.** No agent receives them (the reviewers get
  the file path and the numbered diff; the architecture agent gets changed files and related files).
  A case could not fail.
- **Hunk header text after `@@`: no case.** `app/diff/parse.py` does not render it.
- `evals/redteam.py` now counts `replacement_code` in `hijack_model`; before, a canary placed there
  was invisible to that metric.

## Commands

```
uv run python -m evals.lint_dataset evals/holdout/data

uv run python -m evals.run --suite golden --data-dir evals/holdout/data \
    --agents security,quality --runs 3 --max-cost 5
uv run python -m evals.run --suite pr --data-dir evals/holdout/data \
    --agents security,quality --runs 3 --max-cost 5
uv run python -m evals.run --suite redteam --redteam-dir evals/holdout/redteam \
    --label holdout_v5 --runs 3
uv run python -m evals.architecture_run --set holdout --runs 3 --confirm-spend
```

`--agents security,quality` is required: the golden suite defaults to security only, and the
runner scores a case only against the agents that ran, so quality cases would be skipped silently.
Estimated cost, from the 2026-11-04 run: about $0.75 per pass of the first two commands (both
agents), $0.15 for red team. These are estimates, not measurements.

## Known issue with the golden set (`evals/data/`)

Its "clean" cases are clean for the security agent only. In the 2026-11-04 both-agents run, 15
false positives were scored against the quality agent, and several are real defects in code that
was written as a security example: `clean_param_query` and `inj_sql_fstring` never close the SQLite
connection, `clean_env_secret` and `sec_api_key` ignore the HTTP status. Counting them as false
positives is what pulls quality precision down to 0.35, so that number is part measurement of the
model and part label error. Do not use it as the baseline for the quality agent. Fix: relabel those
cases (add the real defects as expected findings, or fix the code), then re-measure.
