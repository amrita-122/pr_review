# PR Review Agent

A GitHub App that reviews pull requests with LLM agents. On a `pull_request` webhook it
fetches the diff, runs LangGraph review agents (security first, code quality later),
validates every finding, and posts one review with inline comments.

The bot only ever comments (`event=COMMENT`). It never approves, requests changes or merges.
All PR content is treated as untrusted input.

**Status:** Phase 0 (foundations) done. Phase 1 (webhook walking skeleton) is next.

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

## Architecture

Coming with Phase 1.

## Eval results

Coming with Phase 4.

## Limitations

Coming as the project grows.
