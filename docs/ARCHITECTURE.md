# Architecture (living document — update when a phase changes it)

## Flow
GitHub PR event
  → POST /webhooks/github  (web service)
      verify HMAC over raw body → ONE transaction { delivery id (dedupe) + review run
      + jobs row + supersede older runs of the PR } → 202            — app/jobs/queue.py
  → worker (separate process, `python -m app.jobs.worker`)         — app/jobs/worker.py
      claim job (FOR UPDATE SKIP LOCKED, short tx) → run_review_job (deadline 8 min) → done |
      retry with backoff | failed (+ one static "review failed" comment); stale leases requeued
  → run_review_job — app/jobs/handlers.py
      installation token → fetch changed files → filter → parse/number lines
  → review_diff(files)                      ← pure core, no GitHub/HTTP
      [LangGraph: limits -> per-file LLM review -> validate; fake reviewer kept for tests]
      triage (optional: rules, then haiku) → security ∥ quality per file (cheap reviewer for low-risk files)
      validate → aggregate (merge duplicates, sort by severity, cap 25)  — app/review/triage.py, aggregate.py
      validate (file reviewed, line commentable, output policy)
  → before posting: still the PR's head? (GET /pulls/{n}) · already posted? (run.posted_at, then a
    GET /pulls/{n}/reviews scan for this run's hidden marker) — a stale or duplicate review is never posted
  → post ONE review (event=COMMENT)
  → record run, findings (posted or dropped + reason), tokens, estimated cost (best effort)
      if GitHub 422s the inline comments, retry once with the summary only

## Components
| Module | Responsibility |
|---|---|
| app/github/webhooks.py | signature check, event filtering, payload parsing |
| app/github/auth.py | App JWT, installation tokens (cached) |
| app/github/client.py | REST calls: files (paginated), comments, reviews, permissions |
| app/diff/ | filters.py skip rules; parse.py commentable lines + numbered rendering |
| app/review/ | schemas, validate, render, fake reviewer (P2); prompts, graph (P3) — no IO besides the model |
| app/db/ | models, sessions |
| app/logging.py | structlog JSON config; run context bound with contextvars |
| app/review/cost.py | token usage -> estimated USD (pure) |
| evals/ | golden set (data/), runner (run.py), matching + metrics (metrics.py), thresholds.json; red-team set from Phase 5 |

## Data model (planned; columns added in the phase noted)
- webhook_deliveries (P1): delivery_id PK, event, action, received_at
- review_runs (P4, P6 implemented): id = run_id, repo, pr_number, head_sha, status (queued|running|ok|failed|
  superseded), prompt_version, model, timings, tokens_in, tokens_out, cost_usd (accumulated over attempts), error,
  failure_reason (timeout|github|model|internal), posted_at, failure_comment_posted ·
  unique (repo, pr_number, head_sha)
- findings (P4, implemented): id, run_id FK, file, line, severity, category, title, posted, drop_reason
- jobs (P6, implemented): id, kind, payload (JSONB: only run_id, installation, owner, repo, number, head_sha),
  status (queued|running|done|failed), attempts, max_attempts, run_after, locked_at, locked_by, last_error
- review_runs (P8 columns): statuses awaiting_confirmation | confirming | expired; installation_id, awaiting_since,
  pending_comment_posted, expired_comment_posted, confirmed_by, confirmed_at
- jobs.kind `confirm_review` (P8): payload run_id, installation, owner, repo, number, comment_id, commenter_login
- LangGraph checkpoint tables (P8): checkpoints, checkpoint_blobs, checkpoint_writes, checkpoint_migrations —
  created by the checkpointer's own `setup()` at worker start, ignored by Alembic (`include_object`)

## Human confirmation (Phase 8, behind CONFIRMATION_ENABLED; ADR 0007)
  review_diff(..., checkpointer, thread_id=run_id) -> gate after validate: a held severity (default critical)
    -> interrupt -> run awaiting_confirmation -> ONE static "N pending, a maintainer can reply /confirm" comment
  issue_comment `created` "/confirm" -> webhook (HMAC, PR comment, not a Bot, exact body, a run is waiting)
    -> job confirm_review -> worker: GET collaborator permission (admin|write, fail closed)
    -> claim awaiting->confirming (atomic) -> resume graph -> _post_once -> ok -> delete checkpoint thread
  worker loop, once a minute: held > CONFIRM_TTL_HOURS (72) -> expired, thread deleted, one static comment
  A newer push supersedes a held run. The web service never touches the checkpointer; only the worker does.

## Trust boundaries
- Trusted: our code, prompts, config, env secrets.
- Untrusted: everything inside the PR (code, comments, strings, file names, title, description,
  commit messages) and the model's output (it may have been influenced by the PR).
- Also untrusted: the text of `/confirm` comments and who sent them until the worker has checked their
  repository permission with GitHub (the payload's `author_association` is never used).
- Hence: model output is validated before anything is posted; the App can only comment.

## Observability and evals (Phase 4)
- `run_id` (uuid) is created per job, bound into every log line (structlog contextvars), stored as
  `review_runs.id` and attached to the LangSmith trace metadata.
- Token usage comes from a LangChain `UsageMetadataCallbackHandler` passed to `review_diff(callbacks=...)`.
  Cost is computed at the edge (job handler, eval runner), so `review_diff` stays pure.
- Evals: `evals/run.py` -> `build_default_reviewer` -> `review_diff` on each golden case -> match against
  `expected.jsonl` -> metrics -> `evals/results/<date>_<prompt_version>.json`. CI runs it with `--check`.
