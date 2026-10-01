---
name: mentor-review
description: Review Amrita's changes like a senior engineer. Use when she asks for a review, says "check my code", or before she commits a feature.
---
# Mentor review

1. Run `git diff` and `git diff --staged`; if both are empty, review the last commit with `git show`.
2. Read the architecture rules in CLAUDE.md and the current step in TASKS.md.
3. Review in priority order: correctness → security (untrusted input, secrets, signatures, output policy)
   → failure handling & edge cases → tests → design (pure core / IO at edges) → readability.
4. At most 8 comments, each: file:line, severity (blocking / should fix / nit), what's wrong, why it matters.
5. Don't rewrite her code. For blocking issues, a fix sketch of ≤10 lines is fine.
6. If something is genuinely solid, say so in one factual line. No general praise.
7. End with: ready to commit? yes/no + a conventional commit message.
