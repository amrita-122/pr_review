# ADR 0010 — Check Run with annotations

Status: Proposed (needs the owner's approval and a sandbox check; the code is behind `CHECK_RUN_ENABLED`, default off) · Date: 2026-11-11

## Context
Inline review comments are easy to miss and cannot be filtered in the PR's "Checks" tab. A Check Run shows a status next
to the commit and lists findings as annotations in the Files view. Creating one needs the GitHub App permission
`checks: write`, which CLAUDE.md says needs an ADR. A Check Run is as public as a PR comment (ADR 0008 rejected it as a
private channel for exactly that reason).

## Decision
- After a review has been POSTED, create one completed Check Run on the head commit, best effort: a failure (for example
  the permission was not granted: 403) is logged and never fails or retries the review.
- **The conclusion is always `neutral`.** The bot is advisory; it must never block a merge or look like a required check
  (the same reason it only ever posts `COMMENT` reviews).
- **Annotations repeat only what is already posted inline**: the findings in `outcome.findings`. Nothing else. So the
  check adds no information that the inline comments do not already carry.
  - On a PUBLIC repo that already excludes high/critical security findings (ADR 0008: they never reach `findings`), so
    they cannot reach an annotation either. The check's summary is the neutral closing line only.
  - Annotation text is the same title and rationale that passed the output policy; nothing new is generated.
  - Findings on deleted lines (`side="LEFT"`) are not annotated (annotations only address the new file).
- Needs `checks: write`. The App never gains write access to repository contents, never approves, requests changes or
  merges (unchanged).

## Consequences
+ Findings are visible in the Checks tab and the Files view without a new service.
- One more permission for installers to accept; without it the feature silently does nothing (logged).
- A check on a public repo is one more public surface: any future change to what the bot posts must update this ADR's
  "only what is posted inline" rule and its test (`tests/test_check_run.py`, unique-title leak test).
- Re-created on every posted review; old checks stay on old commits. No update-in-place.
