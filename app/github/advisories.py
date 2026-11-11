"""The private channel for public repos (ADR 0008): a DRAFT repository security advisory.

A draft is visible only to the repository's security managers and admins and to collaborators on
the advisory, never to the PR's readers. Needs the App permission "Repository security
advisories: write" (an extra permission: see ADR 0008, which is Proposed until it is tried on a
real public repo). Never raises for content reasons: callers treat any exception as "not
delivered" and fail closed (the finding is kept in the database and NOT written to the PR).
"""

from app.github.client import _headers  # one place builds auth headers
from app.github.http import github_client
from app.review.schemas import Finding

MAX_SUMMARY_CHARS = 1024  # GitHub's limit for the summary


def advisory_body(
    finding: Finding, pr_number: int, head_sha: str, location: str | None = None
) -> dict[str, object]:
    """The request body. Text comes from the model but has passed the output policy (no links,
    mentions or HTML); the location lines are ours."""
    parts = [
        finding.rationale,
        "",
        f"Location: {location or f'`{finding.file_path}` line {finding.line}'} "
        f"(pull request #{pr_number}, commit `{head_sha[:12]}`).",
    ]
    if finding.suggestion:
        parts += ["", f"Suggestion: {finding.suggestion}"]
    parts += ["", "Reported automatically by the PR review bot. Review before acting."]
    return {
        "summary": finding.title[:MAX_SUMMARY_CHARS],
        "description": "\n".join(parts),
        "severity": finding.severity.value,
        # GitHub requires a vulnerabilities entry; a code finding has no package.
        "vulnerabilities": [{"package": {"ecosystem": "other", "name": "source code"}}],
    }


async def create_draft_advisory(
    token: str,
    owner: str,
    repo: str,
    finding: Finding,
    pr_number: int,
    head_sha: str,
    location: str | None = None,
) -> None:
    """Create ONE draft advisory. Raises httpx.HTTPStatusError on 403/404/422 (no permission,
    advisories unavailable, rejected body): the caller decides what that means."""
    async with github_client() as client:
        response = await client.post(
            f"/repos/{owner}/{repo}/security-advisories",
            json=advisory_body(finding, pr_number, head_sha, location),
            headers=_headers(token),
        )
    response.raise_for_status()
