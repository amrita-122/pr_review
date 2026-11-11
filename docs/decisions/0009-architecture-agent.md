# ADR 0009 — Architecture agent (context beyond the diff)

Status: Accepted 2026-11-10 (step 10.22; details below) - Date: 2026-11-10

## Context
The security and quality agents only see one file's diff. Some weaknesses live in the design: missing authorization
layers, unsafe defaults, secrets handling, a trust boundary crossed without validation. The owner wants the bot to
understand the code and look for these.

## Options considered
1. Whole-repo embeddings / vector DB: powerful, but CLAUDE.md forbids vector DBs without an ADR, and it is heavy.
2. Fetch a capped set of related files per PR through the Contents API (read-only, permission already held) and give
   them to one extra agent as data. Simple, bounded cost.
3. Clone the repo in the worker: more context, but needs disk, git, and runs untrusted content closer to our host.

## Decision
- Option 2. Retrieval happens at the edge (job handler), so `review_diff` stays pure and receives the files as data.
- Candidate files: files imported by or next to the changed files, plus fixed names (auth/config/settings/routes
  entry points). Hard caps on file count and characters, like the existing size caps.
- Repo files are UNTRUSTED: same randomized delimiters, the same output policy, and red-team cases for injection
  inside repo files before this is enabled.
- Findings have no line number: they appear in the summary (private repo) or the private channel (public repo,
  high/critical, ADR 0008). They never use inline comments.
- New agent `architecture`, prompt `architecture_v1.md`, setting `ARCHITECTURE_AGENT_ENABLED` (default false).
- Enabled by default only after golden and red-team numbers justify it (precision/recall and cost per PR in README).

## Details decided in 10.22-10.24
- One model call per PR (not per file). Its input is ONE untrusted block: the changed files' numbered diffs
  (capped) plus the related repo files (capped), under the same random per-call markers as the other agents.
- Related files = what the changed Python files import, plus fixed names (settings, config, auth, routes, ...).
  Candidate paths come from untrusted diffs, so they are validated (safe characters only, no `..`) before they
  go into a URL, and every fetch is `GET /repos/{owner}/{repo}/contents/{path}?ref=<head sha>` (read-only).
- Caps (settings): 8 files, 8,000 characters each, 40,000 in total; a 404, a directory, a binary or an oversized
  file is skipped silently. Retrieval never fails the review.
- A finding is `ArchitectureFinding`: severity, title, rationale, optional suggestion, up to 5 related files that
  must be among the changed or fetched files. No line, so it never becomes an inline comment. It passes the same
  output policy as every finding (it is mapped onto a `Finding` for the check).
- Delivery: private repo -> a short "Architecture notes" section in the review summary. Public repo ->
  high/critical go to the private channel (ADR 0008, fail closed), medium/low go to the summary.
- Stored as `findings` rows with category `architecture`, line 0, `posted=false` (never inline); the same title is
  not repeated on a later push of the same PR.
- Off by default (`ARCHITECTURE_AGENT_ENABLED=false`). It turns on by default only after the golden and red-team
  numbers (`evals/architecture/`) are measured and recorded: until then it is an opt-in experiment.

## Consequences
+ Finds issues no single diff shows.
- More tokens per PR; more untrusted input; the quality agent's 35% precision shows new agents can be noisy.
- Retrieval heuristics will miss some relevant files.

## Revisit when
Measured precision stays low, or whole-repo context (option 1) becomes necessary.
