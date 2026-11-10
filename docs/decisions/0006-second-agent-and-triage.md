# ADR 0006 — Second agent, aggregation and cost routing

Status: Accepted · Date: 2026-11-03

## Context
Reviews covered security only. Broader review means more model calls; most PRs include files that need little
scrutiny. Both new parts add untrusted-input surface.

## Decision
- **Quality agent** (`quality_v2`) runs next to the security agent on every file, with the same defences
  (random delimiters, sanitising, output policy, no links). `Finding.agent` is set by the graph node, never by the
  model; the validator drops a category outside its agent's territory (`AGENT_CATEGORIES`).
- **One review, merged**: duplicates (same file + category within 2 lines, or both agents on the same line) are
  merged keeping the most severe (security wins ties); sorted by severity; capped at 25 inline comments; the summary
  states merged and cut counts. (Your choice: merged and sorted, not "quality in the summary only".)
- **Triage = rules first, then a cheap model.** Dangerous patterns and sensitive paths force HIGH and cannot be
  overridden; docs/tests/assets with harmless content are LOW; only the rest is sent to `claude-haiku-5-5`, which
  returns one enum. Any failure means HIGH. Triage is off by default (`TRIAGE_ENABLED`).
- **Low-risk files are still reviewed, by the cheap model** (your choice), so a mislabelled file gets a weaker
  review instead of none; skipping them would turn triage into a suppression channel.
- **Cost is priced per model** (`PriceTable`), since usage metadata is per model.
- **Evals**: `--agents`, `--triage`, `--suite pr`, per-agent precision/recall. The golden gate still runs the
  security agent only until quality cases are labelled.

## Consequences
+ Broader reviews at one comment cap; injection resistance re-measured with both agents (0 hijack, 0 suppression).
- Triage showed no saving on the golden set (29 of 31 files are risky by design); the benefit is unproven until
  multi-file PR cases are labelled. Quality-agent precision is unmeasured (no quality labels yet) and it does flag
  small genuine issues in clean demo code.
- Enabling both agents doubles review cost per file (about $0.013 per golden case vs $0.0068).

## Revisit when
Quality/PR cases exist: decide whether to enable triage by default, and whether the quality agent needs a
severity floor.
