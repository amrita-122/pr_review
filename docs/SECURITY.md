# Security & prompt-injection notes (filled in during Phase 5)

## Threat model
**Attacker:** anyone who can open a PR on a repo where the App is installed.
**They control:** code, comments, string literals, file names, PR title/description, commit messages.
**They want to:**
1. Suppress a real finding (sneak a vulnerability past review)
2. Hijack output (make the bot post attacker text, links, @mentions)
3. Exhaust cost (huge or many files)
4. Extract the system prompt

## Built-in mitigations (by design, from Phase 1–3)
- Least privilege: Pull requests R/W (comments only), Contents R, Metadata R; reviews posted as COMMENT only
- Validator: findings must point at reviewed files and commentable lines
- Size caps on files and characters

## Red-team results
| Defence set | Suppression rate | Hijack rate | Notes |
|---|---|---|---|
| Baseline | | | |

## Residual risks
