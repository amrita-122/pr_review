"""Which findings may appear on a public PR. Pure: no IO.

ADR 0008: this is a security tool, so it must not publish vulnerability details on a public page.
On a PUBLIC repo, high and critical SECURITY findings are diverted to a private channel and never
become PR text. Everything else (medium and low, and every code-quality finding) stays inline.
"""

from app.review.schemas import Agent, Finding, Severity, agent_for_category

PRIVATE_DELIVERED = "delivered privately (public repo)"
PRIVATE_FAILED = "private delivery failed"
PRIVATE_SEVERITIES = frozenset({Severity.HIGH, Severity.CRITICAL})


def is_private_only(finding: Finding) -> bool:
    """True for findings that must never be written on a public PR."""
    return (
        finding.severity in PRIVATE_SEVERITIES
        and agent_for_category(finding.category) == Agent.SECURITY
    )


def divert(findings: list[Finding], public: bool) -> tuple[list[Finding], list[Finding]]:
    """(findings that may go on the PR, findings for the private channel)."""
    if not public:
        return findings, []
    rest = [f for f in findings if not is_private_only(f)]
    private = [f for f in findings if is_private_only(f)]
    return rest, private
