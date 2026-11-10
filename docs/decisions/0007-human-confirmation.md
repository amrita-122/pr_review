# ADR 0007 — Human confirmation for critical findings (`/confirm`)

Status: Accepted · Date: 2026-11-04 (approved by Amrita 2026-11-05)

## Context
A review with critical findings was posted at once. In a public repo that can announce an unpatched vulnerability
before a maintainer has seen it, and LLM output goes out under the bot's name with no human check.

## Decision
- **Hold the whole review** when any finding has a held severity (default: critical). Post only a static comment:
  "N critical finding(s) pending. A maintainer can reply `/confirm`." Reviews without held findings are unchanged.
  One review per run is kept (no split posting), so the marker-based idempotency from ADR 0005 still works.
- **Mechanism:** a `gate` node after `validate` calls LangGraph `interrupt()`. State is saved by the Postgres
  checkpointer (`langgraph-checkpoint-postgres`), `thread_id = run_id`. The model is not called again on resume.
  `review_diff` stays pure: checkpointer and thread id are optional parameters (evals pass neither).
- **Confirm path:** the `issue_comment` webhook (HMAC-verified like every event) accepts only `created` comments on
  a PR, from a non-Bot user, whose body is exactly `/confirm`, and only when the PR has a run awaiting confirmation.
  It enqueues a `confirm_review` job in the same transaction as the delivery record.
- **Authorization:** the worker asks GitHub for the commenter's collaborator permission and proceeds only for
  `admin` or `write`. Fail closed: 404 or any other value = ignore; a GitHub error = retry, never post.
- **Exactly once:** an atomic `awaiting_confirmation -> confirming` update claims the resume. Posting reuses
  `_post_once` (current-head check, `posted_at`, hidden marker). A failed confirm job returns the run to
  `awaiting_confirmation`; it does not fail the run.
- **If nobody confirms:** expire after 72 h, fail-closed. The run becomes `expired`, the checkpoint is deleted and one
  static comment says to push again. Held findings are never auto-posted. A newer push supersedes a pending run.
- **Retention:** checkpoints contain diffs and findings. The thread is deleted when the run is posted, expired or
  superseded. Never logged.
- **Dependency:** `langgraph-checkpoint-postgres` (pulls `psycopg-pool`; `langgraph-checkpoint` 4.2.0 -> 4.3.1).
  The checkpointer creates its own tables; Alembic ignores them. Feature flag `CONFIRMATION_ENABLED`, default off.

## Rejected
- Split posting (non-critical now, critical later): two reviews per PR, breaks "ONE review".
- Auto-post after N hours: fail-open, defeats the gate.
- Own `pending_reviews` table instead of a checkpointer: simpler, but skips the phase's learning goal.

## Consequences
+ Critical findings reach the PR only after a human with write access agrees.
- Non-critical findings in the same review are delayed too.
- Source code sits in Postgres for up to 72 h per pending run.
- A maintainer can still confirm without reading. `/confirm` spam on a PR with a pending run costs one permission
  call per comment (no per-user rate limit yet).
