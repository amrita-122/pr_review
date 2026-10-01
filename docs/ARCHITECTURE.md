# Architecture (living document — update when a phase changes it)

## Flow
GitHub PR event
  → POST /webhooks/github
      verify HMAC over raw body → dedupe X-GitHub-Delivery → enqueue → 202
  → job (BackgroundTasks in v1, Postgres job worker from Phase 6)
      installation token → fetch changed files → filter → parse/number lines
  → review_diff(files)                      ← pure core, no GitHub/HTTP
      [Phase 7] triage → security agent ∥ quality agent → aggregate
      validate (file reviewed, line commentable, output policy)
  → post ONE review (event=COMMENT) → record run, findings, tokens, cost

## Components
| Module | Responsibility |
|---|---|
| app/github/webhooks.py | signature check, event filtering, payload parsing |
| app/github/auth.py | App JWT, installation tokens (cached) |
| app/github/client.py | REST calls: files, comments, reviews, permissions |
| app/diff/ | skip rules, diff parsing, numbered rendering, commentable lines |
| app/review/ | schemas, prompts, LangGraph graph, validator — no IO besides the model |
| app/db/ | models, sessions |
| evals/ | golden set, red-team set, runner, thresholds |

## Data model (planned; columns added in the phase noted)
- webhook_deliveries (P1): delivery_id PK, event, action, received_at
- review_runs (P4): id, repo, pr_number, head_sha, status, prompt_version, model, timings,
  tokens_in, tokens_out, cost_usd, error · unique (repo, pr_number, head_sha) from P6
- findings (P4): id, run_id FK, file, line, severity, category, title, posted, drop_reason
- jobs (P6): id, kind, payload, status, attempts, run_after, locked_at, last_error
- LangGraph checkpoint tables (P8): managed by the checkpointer library

## Trust boundaries
- Trusted: our code, prompts, config, env secrets.
- Untrusted: everything inside the PR (code, comments, strings, file names, title, description,
  commit messages) and the model's output (it may have been influenced by the PR).
- Hence: model output is validated before anything is posted; the App can only comment.
