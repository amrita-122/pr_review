# Relabel log

One dated line per label changed after the first model run (see README rule 3). Say whether the
model's output was visible when you decided. Report scores both ways.

| Date | Case | Change | Why | Model output visible? |
|------|------|--------|-----|-----------------------|
| 2026-11-13 | hn_literal_eval | clean -> added `error_handling` 11-12 | `ast.literal_eval` also raises `TypeError` (`{[]:1}`, checked by hand), not only ValueError/SyntaxError | yes |
| 2026-11-13 | sub_toctou_mktemp | added `resource_leak` 18 | `mkstemp` file is left on disk if writing rows raises | yes |
| 2026-11-13 | sub_ssrf_redirect | added `bug` 19 | size limit is checked only after the whole body is downloaded | yes |
| 2026-11-13 | qa_leak_thread_per_request | added `error_handling` 14 | an exception in `refresh_status` kills the poller silently | yes |
| 2026-11-13 | pr_csv_export_leak | added `bug` 10 | `DictWriter` fieldnames come from the first row, extra keys raise ValueError | yes |

Decided in one pass after the first run, with the model's findings visible. Scores before and after are in
`evals/holdout/README.md` ("Scores both ways"). The golden-set labels (`evals/data/expected.jsonl`) were fixed the
same day for the known issue: unclosed SQLite connections (`clean_param_query`, `inj_sql_fstring`) and ignored HTTP
status (`clean_env_secret`, `sec_api_key`, `crypto_tls_verify_off`).

Left as they were (arguable, not clear defects), so they still count as false positives when flagged:
`qn_defensive_handlers` (returns True when the thread's task raised), `qn_duplicated_formatters` (`None` value crashes
`.strip()`), `pr_pickle_and_swallow_with_noise` (sleep after the last retry), `qa_err_broad_except_reports_saved`
(same defect as the label, reported under `bug`), `pr_workflow_pwn_request` (secret exposure on line 23, same flaw as
the labelled range, reported under `secrets`).
