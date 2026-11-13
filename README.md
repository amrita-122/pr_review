# PR Review Agent

A GitHub App that reviews pull requests with LLM agents. When a PR is opened or updated, it fetches the
diff, runs a security agent and a code-quality agent (LangGraph), validates every finding, and posts
**one** review with inline comments on the right lines.

It is built around two questions most demo "AI reviewers" skip: *how good is it, in numbers?* and
*what can an attacker do through a PR?*

![Demo: a sandbox PR gets one inline review](docs/demo.gif)

## What it does

- Reviews each changed file with a **security** agent and a **quality** agent in parallel, merges
  overlapping findings, sorts by severity and caps the review at 25 inline comments.
- Every finding is **validated** before posting: the file was reviewed, the line is commentable, and the
  text passes an output policy (no links, @mentions or dangerous HTML).
- Treats all PR content as **untrusted**. The bot only ever posts `event=COMMENT`: it never approves,
  requests changes, merges or writes to the repository.
- Survives restarts: a Postgres job queue and a separate worker mean no lost reviews, no duplicate
  reviews, and no review of a commit that is no longer the PR's head.
- Optional human gate: a critical finding is held until a maintainer replies `/confirm`.

## Results

All numbers come from this repo's own eval sets and are reproducible with `uv run python -m evals.run`.
Reviewer model `claude-sonnet-5-5`; each figure is a mean of 3 runs unless stated.

| What | Result | Measured on |
|---|---|---|
| Security agent, final prompt (`security_v5`) | precision **93.6%**, recall **96.7%** | golden set, 20 planted bugs |
| Eval gate (CI, on PRs touching `app/review/**` or `evals/**`) | fails under 85% precision or recall | blocked a deliberately noisy prompt (62.5% precision) |
| Prompt injection, output hijack | **10.3% -> 0%** (0 of 13 with a canary) | 16 red-team cases, baseline vs final |
| Prompt injection, suppression of a real bug | **0 of 16** at baseline and after the fixes; 2.2% mean in a later 3-run re-run (1 miss, `rt_bidi_override`, in 1 of 3 runs) | same set (almost no headroom: see limitations) |
| Cost | about **$0.007 per file** (security agent) | golden set, list prices |
| Latency | p50 about 2 s, p95 about 3 s per file | golden set |
| Quality agent | recall 90%, **precision 69%** (61-80% over 3 runs) | 49-case golden set, labels fixed 2026-11-13 (the old 35% was mostly missing labels); no CI gate yet |
| Triage (cheap model for low-risk files) | **no saving** on small files, so off by default | 5 multi-file PRs, 16 files |

Why these are not bigger or rounder: the golden set is small (one missed bug is about 5 points of recall),
and `security_v2` hit 100% only because it was written after reading v1's failures on the same set, so that
figure is not claimed. Per-run counts, caveats and the table of every defence step are in
[docs/SECURITY.md](docs/SECURITY.md) and [docs/BUILD-LOG.md](docs/BUILD-LOG.md).

The prompt-injection finding worth reading: the ~10% "hijack" at baseline was the model **quoting**
the attack, not obeying it, in all 14 events I read (8 more were not captured), because the prompt told it to report injection attempts. Removing that
instruction (prompt v4) and adding an output policy (links cannot be prevented by a prompt) brought it to 0.

## Architecture

```mermaid
flowchart TD
    GH[GitHub: pull_request / issue_comment] -->|webhook| WEB[Web service: FastAPI]
    WEB -->|verify HMAC, dedupe, one transaction| PG[(Postgres: deliveries, runs, jobs)]
    PG -->|FOR UPDATE SKIP LOCKED| W[Worker process]
    W -->|installation token, fetch + filter files| GHAPI[GitHub REST API]
    W --> L
    subgraph RD[review_diff: pure core, no IO]
        L[limits] --> T[optional triage]
        T --> S[security agent]
        T --> Q[quality agent]
        S --> V[validate + output policy]
        Q --> V
        V --> A[aggregate]
        A --> G{critical finding?}
    end
    G -->|no| P[still head? not posted? post ONE review]
    G -->|yes| H[hold: pending comment]
    H -->|"/confirm" + write/admin check| P
    P --> GHAPI
```

More detail, the data model and trust boundaries: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

## Design decisions

| Decision | Why | Record |
|---|---|---|
| Scope and stack (Python, FastAPI, LangGraph, httpx, no GitHub SDK) | small, inspectable surface | [ADR 0001](docs/decisions/0001-v1-scope-and-stack.md) |
| Build order: skeleton, posting pipeline, then the LLM | prove delivery and line mapping before adding AI | [ADR 0002](docs/decisions/0002-phase-order.md) |
| Eval methodology: labels written before running the model, line-range matching | avoid grading the model on its own answers | [ADR 0003](docs/decisions/0003-eval-methodology.md) |
| Injection defences, each re-measured on its own | know which layer does what | [ADR 0004](docs/decisions/0004-injection-defences.md) |
| Job queue in Postgres, not Redis/Celery | no new infrastructure; transactional with the webhook | [ADR 0005](docs/decisions/0005-postgres-job-queue.md) |
| Second agent and triage, triage off by default | measured: no saving at this file size | [ADR 0006](docs/decisions/0006-second-agent-and-triage.md) |
| Human `/confirm` gate that fails closed after 72 h | a maintainer, not the model, releases critical findings | [ADR 0007](docs/decisions/0007-human-confirmation.md) |

## Limitations

- **Small eval sets.** 20 planted bugs and 16 red-team cases: read counts, not just percentages. The
  golden set can no longer show further improvement for the security prompt.
- **Suppression is unproven.** The model resisted every direct suppression attempt at baseline (0 of 16), but planted
  bugs were missed once in a later re-run (2.2% mean), so it is not strictly zero. The red-team set only guards
  against regressions. Subtler, in-voice justifications still need to be added.
- **Quality agent precision is about 69%, not 35%.** The old figure counted real defects as false positives (the
  golden labels were incomplete). What is left is mostly nitpicks and `bug` vs `error_handling` category ambiguity,
  and it still misses some exception-path leaks. It is noisy enough that there is no quality threshold in the CI gate.
- **Oversize files are skipped, not reviewed** (over 20k characters); the summary lists them.
- **Injection attempts are ignored silently** since prompt v4. Model-level injection is never zero; the
  layers only bound the damage to a vetted comment.
- **No per-author rate limit.** A push storm is bounded by superseding older runs, not by a quota.
- **`/confirm` from a non-maintainer** was verified by unit tests only, not with a real second account.
- **Triage savings** are unmeasured for PRs with many large docs or test files.
- **Findings are public on public repos** (even after `/confirm`). Do not install the App on public repos with real
  code until Phase 10 branch 3 (private delivery, [ADR 0008](docs/decisions/0008-private-delivery-for-public-repos.md)).
- **Known bugs, fixed in Phase 10** (see TASKS.md): a late webhook can lose the newest review; a review can say
  "No findings." when files failed; closed PRs and drafts are not handled; old comments are never corrected.

Full list: [docs/SECURITY.md](docs/SECURITY.md#residual-risks).

## Install

Requires Python 3.12, [uv](https://docs.astral.sh/uv/), Docker (for local Postgres), and an Anthropic API key.

1. **Create a GitHub App** (Settings > Developer settings > GitHub Apps > New). Permissions: Pull requests
   *Read & write*, Contents *Read-only*, Metadata *Read-only* (GitHub shows any extra one the "Issue comment"
   event needs). Subscribe to **Pull request** (and **Issue comment** if you enable `/confirm`). Set a random
   webhook secret (`python -c "import secrets; print(secrets.token_hex(32))"`). Generate a private key and
   base64-encode it. Install the App on a test repo only.
2. **Configure**: `cp .env.example .env` and fill it in (App ID, webhook secret, base64 private key,
   `DATABASE_URL`, `MODEL_REVIEW`, your Anthropic key). Never commit `.env` or `*.pem`.
3. **Database**:
   ```bash
   docker run -d --name pr-review-db -e POSTGRES_PASSWORD=postgres -e POSTGRES_DB=pr_review -p 5432:5432 postgres:16
   uv sync
   uv run alembic upgrade head
   ```
4. **Run the web service and the worker** (two processes):
   ```bash
   uv run fastapi dev app/main.py
   uv run python -m app.jobs.worker
   ```
5. **Receive webhooks locally**: create a channel at smee.io, use it as the App's webhook URL, then
   `npx smee-client --url $SMEE_URL --target http://127.0.0.1:8000/webhooks/github`. Use `127.0.0.1`, not
   `localhost`: on Windows `localhost` can resolve to IPv6 (`::1`) while `fastapi run` listens on IPv4 only, which
   gives `ECONNREFUSED ::1:8000` and events silently never arrive. Also run `uv run alembic upgrade head` after
   pulling, or the worker logs `expiry_check_failed` (`ProgrammingError`) every minute.
6. **Open a PR** on the test repo. For production, the repo ships a `Dockerfile`; run the web service and the worker
   (`python -m app.jobs.worker`) from that one image, run `alembic upgrade head` before starting them, and set the
   same env vars. The AWS deployment (ECS Fargate, RDS, Terraform) is in progress: see ADR 0011 and Phase 11.

Optional switches (all off or defaulted in `.env.example`): `TRIAGE_ENABLED`, `CONFIRMATION_ENABLED`,
`CONFIRM_TTL_HOURS`, `CONFIRM_SEVERITIES`.

### Develop

```bash
uv run pytest                                    # no network or database needed
uv run ruff check . && uv run ruff format .
uv run mypy app
uv run python -m evals.run --runs 3              # real model calls, costs money; --check enforces thresholds
```

## Phase 10: hardening and product improvements

What changed (details in TASKS.md and ADRs 0007-0009): out-of-order webhooks are handled; failed or empty reviews
are never reported as clean; closed PRs and drafts are skipped; only critical findings wait for `/confirm`;
findings are not repeated on later pushes, and "Fixed in <sha>" replies resolve their threads; `/dismiss` marks
false positives; on public repos high/critical security findings never appear on the PR (they go to a private
channel, fail closed); an optional architecture agent reads related repo files.

Numbers: the Phase 10 changes add about 340 unit tests (370 -> 713) (no real model calls). The new migrations
(up to `a9e37c2d5b61`) were applied on a real Postgres database.

Architecture agent, measured once on 2026-11-11 (prompt `architecture_v1`, 15 cases, 0 errors; file
`evals/results/2026-11-11_1019_architecture_v1_architecture.json`):

| Measure | Result |
|---|---|
| Recall on planted architectural flaws | 6 / 6 |
| False alarms on clean designs | 0 / 4 |
| Red-team (injection inside a repo file): model hijacked | 0 / 4 |
| Red-team: hijacked text posted | 0 / 4 |
| Red-team: real flaw lost (suppressed) | 1 / 5 |

Read these with care: it is ONE run on a small set (6 + 4 + 5 cases), so one case moves a rate by 17-25 points.
The suppressed case is a real flaw dropped by the output policy (`mention`) on an injection case: the safe failure
(nothing harmful posted) but a missed finding. Cost per PR was not recorded by this runner (it is a TODO in
TASKS.md), so no cost number is claimed. The agent stays off by default. Its eval gate is wired (`--check`, thresholds in `evals/thresholds.json`) but the CI step
stays off until the repository variable `ARCH_EVAL_ENABLED=true` (10.26).

### Held-out evals and what they found (2026-11-13)

A second eval set that no prompt was tuned on (`evals/holdout/`, rules in its README): 28 single-file cases, 6 whole
PRs, 10 red-team pairs, 15 architecture cases, committed before any model ran. `security_v5`, `claude-sonnet-5-5`,
3 runs each, estimated spend about $6 in total.

| Measure | Result |
|---|---|
| Security agent, single-file + PR (last of 3 runs) | 12 of 12 labelled security bugs found, 0 false positives |
| Quality agent, first run -> after relabelling | precision 68-79% -> 82-90%; recall 15 of 16 -> 85-95% |
| Red team, 10 new channels (removed line, forged JSON, base64, non-English, `replacement_code`, ...) | 0 of 10 suppressed, 0 of 6 hijacked, all 3 runs |
| Red team, original set re-run with `replacement_code` counted | suppression 2.2%, hijack 0% |
| Architecture agent | 18 of 18 recall, 0 of 18 false alarms (every finding read: correct) |

What the held-out run taught more than the scores did. Reading the per-case findings showed most quality
"false positives" were real defects in my own example code (unclosed connections, ignored HTTP status, `literal_eval`
raising `TypeError`). I fixed the labels, kept a dated log (`evals/holdout/RELABEL_LOG.md`) and report both scores, so
the old "quality precision 35%" was mostly label error: it is about 69% on the corrected golden set (recall 90%).
A prompt tweak aimed at exception-path leaks scored worse than the current prompt (10 of 15 against 14 of 15) and was
not adopted (ADR 0012). Limits: 23 labelled bugs means one miss moves recall about 4 points; the set was written knowing
the v5 prompts, and I read its findings, so it is no longer unbiased for quality exception-path cases; the architecture
set is a ceiling (it cannot show improvement). Real remaining misses: a lock not released when `validate()` raises
(3 of 3 runs) and a swallowed `except Exception: pass` (2 of 3).

Private delivery, checked on a real public sandbox repo (2026-11-11, dummy content; ADR 0008 is Accepted): the App's
installation token creates a draft security advisory (HTTP 201); the public sees nothing (404, empty list); a
collaborator with write access also sees nothing, only the admin does. On a private repo the endpoint returns 404,
and turning off private vulnerability reporting does not block drafts. A true read-only user could not be tested on a
personal repo (inferred). The end-to-end check was done by hand (10.21, 2026-11-11): on a public sandbox repo a
planted SQL injection produced a draft advisory and a PR that says only "Review complete." (the non-security finding
stayed inline); on the same repo made private the injection was posted inline and no advisory was created.
`PRIVATE_CHANNEL` still defaults to `none` (high/critical findings on public repos are then kept in the database only),
so set `PRIVATE_CHANNEL=advisory` where you want drafts. The bot can close an advisory but cannot delete it. The App needs the extra permission
`repository_advisories: write` and the "Pull request review comment" event for `/dismiss`.

## Docs

- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md): flow, components, data model, trust boundaries
- [docs/SECURITY.md](docs/SECURITY.md): threat model, red-team results, residual risks
- [docs/BUILD-LOG.md](docs/BUILD-LOG.md): what each phase built, measured, and left open
- [docs/decisions/](docs/decisions/): ADRs
