# PR Review Agent

A GitHub App that reviews pull requests with LLM agents. On a `pull_request` webhook it
fetches the diff, runs LangGraph review agents (security first, code quality later),
validates every finding, and posts one review with inline comments.

The bot only ever comments (`event=COMMENT`). It never approves, requests changes or merges.
All PR content is treated as untrusted input.

**Status:** Phases 0-3 done (deployed security reviewer, an LLM agent in LangGraph). Phase 4 (observability, evals) is next.

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
- The work runs in a FastAPI `BackgroundTasks` job after the 202 is returned. Known limit: a
  restart mid-job loses it (fixed in Phase 6 with a Postgres job queue).
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

What works now: on a sandbox PR the deployed app sends each changed file to a Claude model
(`claude-sonnet-5-5`) through a LangGraph graph, validates the findings and posts ONE review with
inline comments. The fake reviewer is kept for unit tests only.

- `app/review/prompts/security_v1.md`: the reviewer's instructions (what to report, what not to,
  severity levels). It tells the model the diff is untrusted data and that instructions inside it
  must be ignored. Prompts are versioned by file name and never edited in place.
- `app/review/llm.py`: asks the model for a typed `ReviewResult` (structured output, JSON schema).
  The diff is wrapped in `<<<UNTRUSTED_DIFF>>>` markers, and a fake end marker inside the diff is
  stripped. Rate limits, 5xx errors, network errors and timeouts are retried up to 3 times with
  exponential backoff (tenacity); every call has a 60 s timeout. Unparseable output is retried once,
  then that file is marked failed.
- `app/review/graph.py`: `review_diff(files, reviewer, limits)` is the pure core and imports nothing
  from GitHub. Flow: prepare (limits) -> one `review_file` per file in parallel via `Send`
  (5 at a time) -> validate. Findings are collected with an append reducer. One failing file never
  fails the run; it is listed as skipped in the summary.
- Limits (settings): 30 files per PR and 20,000 characters per file; anything over is skipped with a
  reason in the summary.
- Unit tests use fake models and never call a real LLM (an autouse fixture enforces this).

Sandbox results (8 PRs, one issue each): all 5 planted vulnerabilities were caught with a correct
inline comment on the right line.

| Planted issue | Result |
|---|---|
| SQL built with an f-string | critical, `injection`, with the parameterized-query fix |
| Hardcoded live payment key | critical, `secrets` |
| Admin route without auth | critical, `authz` |
| `eval` on user input | critical, `unsafe_exec` |
| MD5 for passwords | high, `crypto` |

Clean PRs: 3 were run (environment-variable key, price helper, safe parameterized query). All
returned "No findings". This is a small sample, not a measurement; Phase 4 builds a proper eval set.

Setup notes: set `ANTHROPIC_API_KEY` (a key scoped to a workspace) and `MODEL_REVIEW`. `claude-sonnet-5-5`
rejects non-default `temperature`, so none is set.

Known limits: results are not yet deterministic (no temperature control); a bad line number from
the model is dropped by the validator rather than corrected; the untrusted-data defence is a prompt
rule plus a marker, not yet measured (Phase 5); a restart mid-job still loses it (Phase 6).

## Architecture

```
GitHub PR event
  -> POST /webhooks/github: verify HMAC -> filter event/action -> dedupe delivery id -> 202
  -> background job: installation token -> fetch changed files -> filter -> parse/number lines
     -> review_diff: one LLM call per file (LangGraph) -> validate -> post ONE review (event=COMMENT)
```

Details and the planned data model are in [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

## Eval results

Coming with Phase 4.

## Limitations

Coming as the project grows.
