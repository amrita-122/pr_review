# ADR 0009 — Architecture agent (context beyond the diff)

Status: Proposed (Accepted when 10.22 starts) · Date: 2026-11-10

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

## Consequences
+ Finds issues no single diff shows.
- More tokens per PR; more untrusted input; the quality agent's 35% precision shows new agents can be noisy.
- Retrieval heuristics will miss some relevant files.

## Revisit when
Measured precision stays low, or whole-repo context (option 1) becomes necessary.
