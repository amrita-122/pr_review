# PR Review Agent

A GitHub App that reviews pull requests with LLM agents. On a `pull_request` webhook it
fetches the diff, runs LangGraph review agents (security first, code quality later),
validates every finding, and posts one review with inline comments.

The bot only ever comments (`event=COMMENT`). It never approves, requests changes or merges.
All PR content is treated as untrusted input.

**Status:** Phases 0 and 1 done (deployed walking skeleton, no AI yet). Phase 2 (diff engine) is next.

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

## Architecture

```
GitHub PR event
  -> POST /webhooks/github: verify HMAC -> filter event/action -> dedupe delivery id -> 202
  -> background job: installation token -> post PR comment
```

Details and the planned data model are in [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

## Eval results

Coming with Phase 4.

## Limitations

Coming as the project grows.
