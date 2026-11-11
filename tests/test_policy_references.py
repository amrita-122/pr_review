"""10.12: output-policy gaps from the audit: bare domains, mailto:, //host, #123, org/repo#45."""

import pytest

from app.review.policy import policy_violation
from app.review.schemas import Category, Finding, Severity


def _f(**fields: str) -> Finding:
    base = {"title": "SQL injection", "rationale": "f-string in query"}
    return Finding(
        file_path="a.py",
        line=2,
        severity=Severity.HIGH,
        category=Category.INJECTION,
        **{**base, **fields},
    )


@pytest.mark.parametrize(
    "text",
    [
        "mail mailto:attacker@evil.example",
        "MAILTO:x@y.z",
        "load //evil.example/x.js",
        "(see //cdn.evil.example)",
        "details at evil.example/fix",
        "visit attacker-site.com",
        "docs.evil.io/guide",
        "sub.domain.example",
    ],
)
def test_bare_domains_mailto_and_protocol_relative_links_are_rejected(text: str) -> None:
    for field in ("title", "rationale", "suggestion"):
        assert policy_violation(_f(**{field: text})) == "output policy: link", (field, text)


@pytest.mark.parametrize(
    "text",
    [
        "same bug as #123",
        "tracked in org/repo#45",
        "see other-org/some.repo#7 for context",
        "fixes GH-99",
        "(#1)",
    ],
)
def test_issue_and_pr_cross_references_are_rejected(text: str) -> None:
    for field in ("title", "rationale", "suggestion"):
        assert policy_violation(_f(**{field: text})) == "output policy: reference", (field, text)


@pytest.mark.parametrize(
    "text",
    [
        "report to security@example.org",  # an address, not a link
        "use os.path.join and config.json and self.name.lower()",
        "a // comment with a space is fine",
        "call `evil.example` literally",  # inside a code span nothing is linked
        "the issue `#123` is quoted in code",
        "use heading # 1 and a#b",
        "CVE-2024-1234 and CWE-89 and line 42",
        "see src/app.py#L10, which is not an issue number",
    ],
)
def test_ordinary_text_still_passes(text: str) -> None:
    assert policy_violation(_f(rationale=text)) is None


def test_every_new_redteam_canary_would_be_dropped_if_the_model_repeated_it() -> None:
    """The red-team cases carry these canaries; a model that obeyed the planted note would put
    them in its text. Offline proof that the policy drops each one (the live run measures the
    model's side)."""
    from evals.redteam import load_redteam_cases

    new = {
        "rt_bare_domain",
        "rt_mailto",
        "rt_protocol_relative",
        "rt_issue_reference",
        "rt_repo_reference",
    }
    cases = [c for c in load_redteam_cases() if c.row.case_id in new]
    assert {c.row.case_id for c in cases} == new
    for case in cases:
        for canary in case.row.canaries:
            for field in ("title", "rationale", "suggestion"):
                reason = policy_violation(_f(**{field: f"Also see {canary} for details"}))
                assert reason is not None, (case.row.case_id, field)
