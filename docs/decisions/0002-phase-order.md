# ADR 0002 — Build evals and injection hardening before reliability and the second agent

Status: Accepted · Date: 2026-09-30

## Context
Limited time before the end-of-2026 job-search goal. The parts that make this project stand out are
measured quality (evals + CI gate) and measured prompt-injection resistance.

## Decision
Order: skeleton → diff/posting → security agent → evals + eval CI gate → injection hardening →
reliability (job queue) → second agent + cost routing → human-in-the-loop (optional) → ship.

## Consequences
+ The strongest evidence exists early, even if later phases are cut.
− BackgroundTasks stays in place longer, so restarts can lose a review until Phase 6 (acceptable on a sandbox).
