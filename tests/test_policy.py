import pytest

from app.diff.parse import parse_patch
from app.review.policy import (
    MAX_RATIONALE_CHARS,
    MAX_SUGGESTION_CHARS,
    policy_violation,
)
from app.review.schemas import Category, Finding, Severity
from app.review.validate import partition_findings


def _f(title: str = "SQL injection", rationale: str = "f-string in query", **kw: str) -> Finding:
    return Finding(
        file_path="a.py",
        line=2,
        severity=Severity.HIGH,
        category=Category.INJECTION,
        title=title,
        rationale=rationale,
        suggestion=kw.get("suggestion"),
    )


def test_clean_finding_passes() -> None:
    assert (
        policy_violation(_f(suggestion="Use a parameterized query: `execute(sql, (name,))`"))
        is None
    )


@pytest.mark.parametrize(
    "text",
    [
        "see https://evil.example/fix",
        "HTTP://EVIL.EXAMPLE",
        "visit www.evil.example now",
        "click [here](x)",
        "![audit](p.png)",
        "open javascript:alert(1)",
        "data:text/html;base64,AAAA",
        "ftp://x.example/a",
    ],
)
def test_links_are_rejected_in_every_field(text: str) -> None:
    assert policy_violation(_f(rationale=text)) == "output policy: link"
    assert policy_violation(_f(title=text)) == "output policy: link"
    assert policy_violation(_f(suggestion=text)) == "output policy: link"


def test_link_inside_a_code_span_is_still_rejected() -> None:
    assert policy_violation(_f(rationale="use `https://evil.example`")) == "output policy: link"


@pytest.mark.parametrize("text", ["contact @attacker", "@team please look", "cc:@a1"])
def test_mentions_are_rejected(text: str) -> None:
    assert policy_violation(_f(rationale=text)) == "output policy: mention"


def test_mention_inside_code_is_allowed_and_email_is_not_a_mention() -> None:
    assert policy_violation(_f(rationale="add the `@login_required` decorator")) is None
    assert policy_violation(_f(rationale="```\n@app.route('/x')\n```")) is None
    assert policy_violation(_f(rationale="report to security@example.org")) is None


@pytest.mark.parametrize(
    "text", ["<script>x</script>", "a <img src=x> b", "<!-- hidden -->", "</div>", "< b>bold"]
)
def test_html_is_rejected(text: str) -> None:
    assert policy_violation(_f(rationale=text)) == "output policy: html"


def test_html_in_code_and_comparisons_are_allowed() -> None:
    assert policy_violation(_f(rationale="renders `<script>` from user input")) is None
    assert policy_violation(_f(rationale="when a < 5 and b > 3 the check is skipped")) is None


def test_invisible_characters_are_rejected() -> None:
    assert policy_violation(_f(rationale="fine​ text")) == "output policy: invisible characters"
    assert policy_violation(_f(title=chr(0xE0041) + "x")) == "output policy: invisible characters"


def test_length_limits() -> None:
    assert policy_violation(_f(rationale="x" * MAX_RATIONALE_CHARS)) is None
    assert policy_violation(_f(rationale="x" * (MAX_RATIONALE_CHARS + 1))) == (
        "output policy: rationale too long"
    )
    assert policy_violation(_f(suggestion="x" * (MAX_SUGGESTION_CHARS + 1))) == (
        "output policy: suggestion too long"
    )


def test_validator_drops_policy_violations_with_a_reason() -> None:
    parsed = parse_patch("a.py", "@@ -0,0 +1,2 @@\n+import os\n+x = 1")
    good, bad = _f(), _f(rationale="see https://evil.example")
    kept, dropped = partition_findings([good, bad], {"a.py": parsed})
    assert kept == [good]
    assert [(f, reason) for f, reason in dropped] == [(bad, "output policy: link")]
