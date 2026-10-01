---
name: next
description: Explain the next unchecked step in TASKS.md just-in-time. Use when Amrita says "next", "what now", "what's the next step", or starts a session.
---
# Next step

1. Read TASKS.md (current phase + first unchecked step), docs/ARCHITECTURE.md, and look at the
   relevant existing code.
2. If the previous step looks finished (code exists, tests pass), say so and tick it in TASKS.md.
3. Explain the next step:
   - **What** you're building, in 1–2 sentences.
   - **Why** it exists, in terms of this project (what breaks without it).
   - **Concepts** it needs, each in 2–4 sentences: just enough to do the step.
   - **How**: numbered sub-steps in plain language, naming the exact files, library functions,
     API endpoints and commands. Snippets only if a concept can't be explained in words (≤15 lines).
   - **Gotchas**: the 1–3 mistakes most likely to bite here.
   - **Done when**: how to verify it works (test to write, command to run, what to see on GitHub).
4. Don't write the implementation. Don't ask quiz questions. Stop and let her build.
