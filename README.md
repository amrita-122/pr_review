# PR Review Agent

A GitHub App that reviews pull requests with LLM agents. On a `pull_request` webhook it
fetches the diff, runs LangGraph review agents (security first, code quality later),
validates every finding, and posts one review with inline comments.

The bot only ever comments (`event=COMMENT`). It never approves, requests changes or merges.
All PR content is treated as untrusted input.

**Status:** Phases 0-4 done (deployed LLM security reviewer, structured logs, per-run records, golden-set evals and an eval CI gate). Phase 5 (prompt-injection hardening) is next.

## Phase 0 — Foundations

What works now:

- `GET /health` returns `{"status": "ok"}` (FastAPI).
- A pytest test calls it through FastAPI's `TestClient`.
- ruff (lint + format) and mypy are configured in `pyproject.toml`.
- GitHub Actions runs ruff, mypy and pytest on every pull request and every push to `main`.

### Run it locally

Requires Python 3.12 and [uv](https://docs.astral.sh/uv/).

```bash
uv sync                                  # install dependencies
uv run fastapi dev app/main.py           # dev server; open /health and /docs
uv run pytest                            # tests
uv run ruff check . && uv run ruff format .
uv run mypy app
```

## Phase 1 — Walking skeleton (no AI)

What works now: the deployed app (Railway) receives a real `pull_request` webhook from the
GitHub App, verifies it, ignores duplicates, and comments "Review bot received this PR".

- `POST /webhooks/github` checks the `X-Hub-Signature-256` HMAC over the raw body
  (`hmac.compare_digest`); missing or wrong signature → 401.
- Only `pull_request` events with action `opened`, `reopened` or `synchronize` are handled;
  everything else returns 202 and is ignored. Malformed payloads → 422.
- Idempotency: the `X-GitHub-Delivery` id is inserted into Postgres (`webhook_deliveries`,
  primary key) before any work. A redelivery hits the key, returns 202 `duplicate`, and does nothing.
- In Phase 1 the work ran in a FastAPI `BackgroundTasks` job after the 202 (a restart lost it; fixed in
  Phase 6 with a Postgres job queue, see below).
- GitHub App auth: App JWT (RS256) → installation access token, cached until 5 minutes before expiry.
- Dockerfile + `railway.json`; migrations run as the pre-deploy command `alembic upgrade head`.

### Local setup

```bash
cp .env.example .env                     # fill in the GitHub App values
docker run -d --name pr-review-db -e POSTGRES_PASSWORD=postgres -e POSTGRES_DB=pr_review   -p 5432:5432 postgres:16               # use another host port if 5432 is taken
uv run alembic upgrade head              # create tables
uv run fastapi dev app/main.py
npx smee-client --url $SMEE_URL --target http://localhost:8000/webhooks/github
```

Unit tests need no database or network: they use in-memory SQLite and respx.

## Phase 2 — Diff engine + posting pipeline (fake reviewer, no AI)

What works now: on a sandbox PR the deployed app fetches the changed files, skips what shouldn't be
reviewed, finds the lines GitHub can attach comments to, runs a fake reviewer, validates its
findings and posts ONE review with inline comments on the right lines.

- `list_pr_files` calls `GET /pulls/{n}/files` with `per_page=100` and follows the `Link` header
  (capped at GitHub's 3000-file limit). Files without a `patch` (binary / too large) are skipped
  with a reason.
- `app/diff/filters.py`: one list of skip rules (lockfiles, minified files and source maps,
  vendored/generated folders, generated code, binaries, deleted files).
- `app/diff/parse.py` (unidiff): per file, the set of commentable new-file lines (added + context)
  and a numbered rendering (`L42 + code` / `L43   code`). A file path containing a newline is rejected
  because it could forge diff headers.
- `app/review/schemas.py`: `Severity`, `Category`, `Finding`, `ReviewResult` (Pydantic).
- `app/review/validate.py`: a finding is kept only if its file was reviewed and its line is
  commentable. Dropped findings are logged with the reason.
- `app/review/fake.py`: regex reviewer (`TODO`, `password =`) with the same signature the LLM
  reviewer will have: one file's numbered diff -> findings.
- `post_review` sends `POST /pulls/{n}/reviews` with `commit_id` = head SHA and `event=COMMENT`
  (hardcoded: the bot never approves or requests changes). No findings -> summary only. If GitHub
  rejects the inline comments with a 422, the job retries once with the summary only, so a bad
  line can't lose the whole review.
- Real diffs from sandbox PRs are saved in `tests/fixtures/diffs/`; they seed the eval set later.

Checked on the deployed app: a PR adding `db_password = "hunter2"` and a TODO got inline comments on
exactly those two lines; lockfile and image changes were listed as skipped in the summary.

Known limits: a restart mid-job still loses it (Phase 6); the fake reviewer is regex only; the
`Link`-header cap means PRs over 3000 files are only partly reviewed.

## Phase 3 — First LLM agent (security)

What works now: the background job runs a LangGraph security reviewer (Anthropic chat model)
instead of the fake reviewer. The fake reviewer is kept for tests.

- `app/review/prompts/security_v1.md`: the reviewer prompt (versioned by file name, never edited
  in place). It says what to look for, what not to report, the severity definitions, and that the
  diff between the markers is untrusted data.
- `app/review/llm.py`: `make_reviewer` wraps a chat model with `with_structured_output(ReviewResult)`.
  Input is one file's numbered diff plus its path, wrapped in `<<<UNTRUSTED_DIFF>>>` markers (a
  closing marker inside the diff is stripped so it can't end the block early). Output that doesn't
  parse is retried once, then that file is marked failed.
- Reliability: tenacity retries rate limits, 5xx, network errors and timeouts with exponential
  backoff (max 3 attempts); every model call has a timeout (`MODEL_TIMEOUT_SECONDS`, default 60s).
  The SDK's own retries are off so tenacity is the only place that decides.
- `app/review/graph.py`: `prepare` (apply limits) -> one `review_file` per file via `Send`
  (parallel, at most 5 at once) -> `validate` -> END. State uses append reducers for findings and
  errors. `review_diff(files, reviewer, limits)` has no GitHub imports, so evals can call it directly.
- Limits: `MAX_FILES_PER_PR` (default 30) and `MAX_CHARS_PER_FILE` (default 20000). Files over a
  limit, and files whose review failed, are skipped and listed in the review summary. One failing
  file never fails the run.
- Unit tests use a fake chat model that returns a fixed `ReviewResult`; no test calls a real LLM.
- Sandbox: a small vulnerable app and PRs that each introduce one issue (SQL built with f-strings,
  hardcoded API key, admin route without auth, `eval` on user input, weak password hashing), plus clean PRs.

Known limits: no precision/recall numbers yet (Phase 4); a restart mid-job still loses it (Phase 6);
the diff is untrusted input and prompt-injection hardening is measured in Phase 5.

## Phase 4 — Observability, evals, eval CI gate

What works now: every review has a `run_id`, a database record with tokens and estimated cost,
JSON logs, a LangSmith trace, and a number for how good the reviewer is.

- **Logs**: `structlog` writes one JSON object per line (`app/logging.py`). The job binds `run_id`,
  repo, PR number and head SHA once, and every log line from the graph, reviewer and validator
  carries them. Diffs, model output and tokens are never logged.
- **Tracing**: LangSmith traces carry repo, PR, head SHA, prompt version, model and `run_id`.
- **Records**: `review_runs` (status, prompt version, model, tokens, estimated cost, error) and
  `findings` (every finding, `posted` or dropped with `drop_reason`). Writing them can fail without
  affecting the review. Cost = tokens x `PRICE_INPUT_PER_MTOK` / `PRICE_OUTPUT_PER_MTOK`.
- **Golden set**: 31 cases in `evals/data/` (20 planted bugs across injection, secrets, authz, crypto,
  unsafe_exec and other; 11 clean, including hard negatives). See [ADR 0003](docs/decisions/0003-eval-methodology.md).
- **Run it**: `uv run python -m evals.run --runs 3` (add `--prompt-version security_v1`,
  `--case <id>`, `--max-cost 3`, `--check`). Results are saved in `evals/results/`.
- **Gate**: `.github/workflows/evals.yml` runs on PRs touching `app/review/**` or `evals/**` and fails
  when mean precision or recall drops under `evals/thresholds.json` (0.85 / 0.85) or any case errors.
  It needs an `ANTHROPIC_API_KEY` repository secret. Proof: a PR with a deliberately noisy prompt
  failed the gate (precision 62.5% against a 0.85 minimum).

Results (3 runs each, `claude-sonnet-5-5`, 31 cases, about $0.55 per 3 runs):

| Prompt | Precision (mean, min-max) | Recall (mean, min-max) | Clean cases with a false positive | Cost per case |
|---|---|---|---|---|
| `security_v1` | 75.7% (73.1-79.2) | 93.3% (90.0-95.0) | 1, 1, 0 of 11 | $0.0054 |
| `security_v2` | 100% (100-100) | 100% (100-100) | 0, 0, 0 of 11 | $0.0059 |

Latency per case: p50 about 2 s, p95 about 3 s. Invalid-line rate: 0% for both prompts.

What changed in v2 and why: v1 reported "no authentication" on any route, reported hardening nits
(constant-time comparison, token expiry), and labelled `shell=True` as injection. v2 limits the
authz rule to sensitive actions, lists what not to report, and fixes the category for each kind of
finding.

Honest caveats: v2 was written after reading v1's failures on this same set, so its 100% is
optimistic and the set can no longer show further improvement. 20 bugs is small (one miss is 5
points of recall). The matching rule depends on the label scheme: v1's remaining recall misses were
mostly the model choosing a different category than the label. Next step is more and harder cases.

Known limits: the diff is untrusted and injection resistance is not measured yet (Phase 5); a restart
mid-job still loses it (Phase 6); run records are not unique per head SHA yet (Phase 6).

## Phase 6 — Reliability

What works now: a review is no longer lost when the server restarts, five quick pushes give one review
on the newest commit, and a review is never posted twice.

- **Queue**: a `jobs` table in the existing Postgres, no new infrastructure. The webhook saves the
  delivery id, the review run and the job in **one transaction**, so a crash cannot record a delivery
  without its job.
- **Worker**: `python -m app.jobs.worker`, a separate process (a second Railway service from the same
  image, config in `railway.worker.json`). It claims jobs with `SELECT ... FOR UPDATE SKIP LOCKED`
  (committed at once; no transaction is held during a review), retries transient failures with
  exponential backoff (30 s, 60 s, ...; 3 attempts), fails permanent ones (GitHub 404, model 400)
  immediately, and requeues jobs whose worker died (10 minute lease). On SIGTERM it stops taking jobs,
  lets the current one finish (30 s), otherwise puts it back without using an attempt.
- **No stale reviews**: a run is unique per (repo, PR, head SHA). A newer push marks older in-flight
  runs `superseded`; right before posting the job asks GitHub for the PR's current head and skips if it
  moved.
- **No duplicate reviews**: `posted_at` on the run, plus a hidden per-run marker in the review body that
  a retry looks for on GitHub, which covers a crash between GitHub accepting the review and our write.
- **Timeouts everywhere**: GitHub 5 s connect / 10 s read (20 s to post), Postgres connect 5 s and
  statement 15 s, model 60 s per call, whole job 8 minutes. Idempotent GitHub reads retry on 5xx / 429 /
  network errors; posting is never blindly retried.
- **Failures are visible but quiet**: the run records a reason (`timeout`, `github`, `model`,
  `internal`) and a short error built from the exception type and HTTP status only. After the final
  failure the PR gets one static comment ("Review failed. Push a new commit to try again."), at most
  once per run and never for a superseded run. Stack traces and exception text are not stored or logged.

Tests: the full pipeline runs against mocked GitHub (retries, backoff, supersede, five rapid pushes,
stale head, double-post guards, crash and shutdown recovery), plus 6 tests on a real Postgres in CI
(8 threads claiming 40 jobs get each exactly once, a locked row is skipped, migrations match the models).

Known limits: the worker polls every 2 s (no LISTEN/NOTIFY); there is no per-author rate limit; the
migration deletes older duplicate runs of the same commit before adding the unique constraint.

## Phase 7 — Second agent and cost routing

What works now: every file is reviewed by two agents in parallel (security and code quality), their
findings are merged into one review, and an optional triage step sends low-risk files to a cheaper
model. Triage is **off by default**: measured on this repo's data it does not pay for itself yet (below).

- **Quality agent** (`quality_v2`): real bugs, error handling and resource leaks in changed lines; not
  style, not security. It shares the Phase 5 defences (random delimiters, invisible-character stripping,
  output policy, no-links rule). `Finding.agent` is set by the graph, never by the model, and the
  validator drops a finding whose category belongs to the other agent.
- **Aggregator** (`app/review/aggregate.py`): findings on the same file and category within 2 lines, or
  from both agents on the exact same line, become one (most severe wins; security wins a tie). The list
  is sorted by severity and capped at 25 inline comments; the summary groups by category and says how
  many findings were merged or cut, so the list is never silently short.
- **Triage** (`app/review/triage.py`): rules first (dangerous patterns such as `eval`, SQL, secrets,
  `verify=False`, and sensitive paths force **high**; docs, tests and assets with harmless content are
  **low**); only files the rules leave open go to `claude-haiku-5-5`, which may answer one word.
  Rules cannot be overridden by anything in the diff, hidden characters are stripped before the rules
  run, and any triage error or odd answer means high. Low-risk files are still reviewed, by the cheap
  model. Turn it on with `TRIAGE_ENABLED=true` (`MODEL_TRIAGE`, `TRIAGE_PROMPT_VERSION`).
- **Cost accounting**: token usage is priced per model (`PRICE_TRIAGE_*` for Haiku), so a mixed review
  is not billed at the expensive rate.
- **Evals**: `--agents security,quality`, `--triage on|off`, `--suite pr` for multi-file pull requests
  (`evals/data/prs/<id>/*.diff`, labels in `expected.jsonl`), and per-agent precision and recall.

Measured (3 runs, `claude-sonnet-5-5` reviewer, `claude-haiku-5-5` triage):

| Golden set, security agent, 31 files | Precision | Recall | $/case | Files sent to the cheap model |
|---|---|---|---|---|
| Triage off | 93.7% | 98.3% | $0.00676 | 0 of 31 |
| Triage on | 96.7% | 98.3% | $0.00691 | 2 of 31 (6.5%) |

| Red-team, both agents (16 cases, 3 runs) | Suppression | Hijack posted |
|---|---|---|
| Triage off | 0 of 16 | 0 of 13 |
| Triage on (incl. the case that tells triage "Risk: LOW") | 0 of 16 | 0 of 13 |

Honest reading:
- **Triage saved nothing here.** The golden files are small and deliberately full of risky code, so the
  rules send 29 of 31 to the full reviewer, and the triage call itself costs a little (+2%). The savings
  triage is designed for (many docs, tests and trivial files in one PR) need multi-file PR cases to
  measure; those are written by hand and are the open item below. Do not read "+2%" as "triage costs
  more": it is "no cheap files to route".
- **Quality noise is real.** On the same 31 single-file cases the quality agent (v2) reported something in
  10 of them, including 4 of the 11 clean ones, mostly small but genuine issues in demo snippets (an
  unclosed connection, an ignored HTTP status). Those cases carry security labels only, so this says
  "it finds things", not how precise it is. Quality precision and recall need hand-labelled quality cases.
- The injection hijack rate stayed at 0 with the quality agent and with triage on, and the triage-aimed
  injection did not move a dangerous file to the cheap model (the rules caught it).

Open: add quality cases and multi-file PR cases to `evals/data`, then run
`uv run python -m evals.run --suite pr --agents security,quality --triage off --runs 3` and the same
with `--triage on` for the cost-per-PR comparison.

## Architecture

```
GitHub PR event
  -> POST /webhooks/github: verify HMAC -> filter event/action -> dedupe delivery id -> 202
     -> ONE transaction: delivery + review run + job (supersedes older runs of the PR)
  -> worker (separate process): claim job -> installation token -> fetch changed files -> filter
     -> review_diff (LangGraph: limits -> optional triage -> security + quality agents per file,
        in parallel) -> validate -> merge, sort, cap
     -> still the PR's head? not posted yet? -> post ONE review (event=COMMENT)
```

Details and the planned data model are in [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

## Eval results

See the Phase 4 section above for the current baseline and how to run it.

## Limitations

Coming as the project grows.
