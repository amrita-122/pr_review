# ADR 0001 — v1 scope and stack

Status: Accepted · Date: 2026-09-30

## Context
Solo portfolio project, built while learning just-in-time, alongside a job search.
Goal: a deployed, measured, reliable PR reviewer rather than a feature-heavy demo.

## Decision
- FastAPI webhook receiver; one Postgres database; LangGraph with 2–3 agents (security first, then code quality).
- GitHub App, not a personal access token: per-repo install, scoped permissions, short-lived tokens.
- No Redis/queue in v1: BackgroundTasks first, a Postgres job table in Phase 6. No new services.
- GitHub REST via httpx directly, no SDK.
- No frontend: the GitHub PR page is the UI.
- Docker on Railway (Render as fallback).

## Consequences
+ Few moving parts, cheap, easy to explain. − Work can be lost on restart until Phase 6. − No dashboard.

## Revisit when
Volume needs multiple workers, or a feature genuinely needs a UI.
