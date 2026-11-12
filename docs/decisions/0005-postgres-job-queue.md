# ADR 0005 — Postgres job queue and a separate worker (replaces BackgroundTasks)

Status: Accepted (the "second Railway service" is now an ECS service, ADR 0011; the queue design is unchanged) · Date: 2026-11-03

## Context
Reviews ran in FastAPI `BackgroundTasks` after the 202. A restart or crash mid-review lost the work (ADR 0002
accepted that "until Phase 6"). Pushing again created a second run for the same commit, and nothing stopped a
review of an old commit from posting after a newer push.

## Decision
- **Queue = a `jobs` table in the existing Postgres.** No Redis, Celery or broker (CLAUDE.md rule).
- **Atomic enqueue:** the webhook saves the delivery id, the review run and the job in ONE transaction. A crash
  can no longer record a delivery without its job; a failed enqueue saves nothing, so GitHub's redelivery works.
- **Claim:** `SELECT ... FOR UPDATE SKIP LOCKED`, committed immediately (no transaction held during a review).
  Any number of workers can run; none takes a job twice (tested on real Postgres with 8 threads).
- **Lease + reaper:** a job stuck in `running` past the lease (10 min) is requeued, or failed if out of attempts.
  The per-job deadline (8 min) is shorter than the lease so a live job is never reaped.
- **Retries:** exponential backoff with jitter (30 s, 60 s, ...), max 3 attempts. Failures are classified from
  the exception TYPE and HTTP status only (timeout / github / model / internal; transient vs permanent). A
  permanent error (e.g. GitHub 404, model 400) fails at once. Exception text is never stored or logged.
- **Idempotent posting:** a run is unique per (repo, PR, head SHA). Before posting, the job asks GitHub for the PR's
  current head (stale -> mark superseded, do not post) and checks `posted_at` and a hidden per-run marker in
  existing reviews (a crash between GitHub's 200 and our write cannot cause a second review).
- **Supersede:** enqueueing a newer commit marks older queued/running runs of the PR `superseded`; a superseded run
  never posts and a retry never revives it.
- **Final failure:** one static comment ("Review failed. Push a new commit to try again."), at most once per run,
  never for a superseded run, never containing error details.
- **Worker = a second Railway service from the same image** (`railway.worker.json`), `python -m app.jobs.worker`.
  SIGTERM: stop claiming, let the current job finish within 30 s, otherwise release it to the queue without
  using an attempt.
- **Timeouts everywhere:** GitHub calls 5 s connect / 10 s read (20 s to post a review); Postgres connect 5 s and
  statement 15 s; model 60 s per call (existing); whole job 8 min.

## Consequences
+ Reviews survive restarts; five quick pushes produce one review on the latest commit; no duplicate posts.
+ Failures are recorded on the run with a reason and surfaced to the PR author in one short comment.
- One more process to run (a second Railway service) and a ~2 s polling delay (no LISTEN/NOTIFY yet).
- The migration deletes older duplicate runs (same repo, PR, commit) before adding the unique constraint.
- Residual: a crash after GitHub accepts a review but before `posted_at` is written is covered by the marker
  scan; a marker scan that itself fails twice would retry the job (bounded by max attempts).

## Revisit when
Throughput needs more than polling can give (use LISTEN/NOTIFY), or jobs of other kinds need priorities.
