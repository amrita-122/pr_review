# LEARNING — concepts implemented (Claude updates when Amrita has implemented it; evidence = commit)

| Concept | Phase | Status | Evidence |
|---|---|---|---|
| uv, pyproject, lockfiles | 0 | Implemented | 6910559 |
| FastAPI routes, dependencies | 0–1 | Implemented | 2119733, 4144209 |
| pytest, TestClient, respx | 0–2 | Learning (Phase 1 tests by owner; Phase 2 tests written with Claude) | 2119733, cae16e8, 4144209, 5fa18ee |
| GitHub Actions CI | 0, 4 | Learning (Phase 0 done; eval gate in Phase 4) | 3029596 |
| Webhooks + HMAC signatures | 1 | Implemented | cae16e8 |
| Idempotency, at-least-once delivery | 1 | Implemented | 4144209 |
| GitHub App auth (JWT → installation token) | 1 | Implemented | 4144209 |
| SQLAlchemy 2.0 + Alembic | 1 | Implemented | 4144209 |
| Docker + Railway deploy | 1 | Implemented | 4144209, ebfc06a |
| Unified diffs, hunks, line mapping | 2 | Implemented with help (Claude wrote the code at the owner's request) | 36505ec |
| GitHub reviews API | 2 | Implemented with help (Claude wrote the code at the owner's request) | 5fa18ee |
| Structured output | 3 | Not started | |
| LangGraph state, reducers, Send | 3 | Not started | |
| Prompt design + versioning | 3 | Not started | |
| Retries, backoff, timeouts | 3 | Not started | |
| Tracing + structured logging | 4 | Not started | |
| Eval design, precision/recall, eval CI gate | 4 | Not started | |
| Prompt injection: threat model, red-teaming, defences | 5 | Not started | |
| Postgres job queue, SKIP LOCKED | 6 | Not started | |
| Parallel agents, aggregation, model routing | 7 | Not started | |
| Checkpointers, interrupt/resume | 8 | Not started | |

Status: Not started → Learning → Implemented with help → Implemented independently

## Gaps found in mock interviews (only if /interview-me is used)
