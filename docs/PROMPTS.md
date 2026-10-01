# PROMPTS

**Claude Code (inside the repo)** → building, reviewing, debugging. It reads CLAUDE.md automatically.
**claude.ai Project "PR Review Agent"** → deeper concept explanations, design discussions,
eval design, README/resume writing, interview practice.

---
## 1. claude.ai Project setup (once)
Create a Project named "PR Review Agent". Upload as project knowledge: CLAUDE.md, TASKS.md,
docs/ARCHITECTURE.md, docs/SECURITY.md, docs/decisions/*. Re-upload TASKS.md and new ADRs when they change.
Paste this as the Project instructions:

> You are my senior AI/backend engineer mentor on my PR Review Agent project. I'm learning
> just-in-time and I write the code myself. The project files (CLAUDE.md, TASKS.md, ARCHITECTURE.md,
> ADRs) are the source of truth — stay consistent with the decisions in them. Explain from first
> principles, then how it's done in production in 2026. Tell me how to build things step by step
> without writing full implementations unless I ask. Point out bugs, security issues and weak
> reasoning directly. If something newer or better exists than what we decided, say so and propose
> an ADR instead of silently changing course. No quizzes or exercises unless I ask.

---
## 2. Claude Code — daily loop
| When | Type |
|---|---|
| Start of session / finished a step | `/next` |
| A step needs a concept you don't know | `/teach <concept>` |
| Wrote code, before committing | `/mentor-review` |
| A design choice comes up | `/adr <the choice>` |
| Want interview practice (optional) | `/interview-me` |

### Stuck (after ~20 minutes of trying)
> Stuck on step <N.N>. Expected: <X>. Actual: <Y>. Error/output: <paste>.
> Tried: <list>. My guess at the cause: <guess>.
> Tell me the root cause and the next debugging step; don't rewrite my code.

### Allowed scaffolding
> Scaffold <Dockerfile / evals.yml / alembic setup / test fixture> for step <N.N>. Explain each part briefly.

### End of a phase
> Check every "Done when" line of Phase <N> against the code, tests and CI. List anything not truly
> done. If all pass: update TASKS.md (current phase), LEARNING.md, and draft the README section for this phase.

### End of session
> Summarise today in 3 lines: what I built, what's half-done, the exact next step. Update TASKS.md ticks.

---
## 3. claude.ai — useful prompts
**Deep concept:** Explain <concept> from first principles, then how it's done in production in 2026,
and the mistakes people make with it — in the context of my PR review agent.

**Design review:** Here's my design for <component>: <describe>. Review it as a tech lead: what breaks
first, what's over-engineered, what an interviewer would challenge.

**Eval / red-team design (Phases 4–5):** Here are my test cases and metrics: <paste>. Would these
catch real failures? What's missing?

**Writing (Phase 9):** My real results: <numbers>. Draft README sections / resume bullets. Add nothing I didn't do.
