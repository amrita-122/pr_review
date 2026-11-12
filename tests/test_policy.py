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
    "text",
    [
        "<script>x</script>",
        "a <img src=x> b",
        "<!-- hidden -->",
        "<a href=x>go</a>",
        "<IFRAME src=x>",
        "< details >",
        "</script>",
    ],
)
def test_dangerous_html_is_rejected(text: str) -> None:
    assert policy_violation(_f(rationale=text)) == "output policy: html"


def test_harmless_tags_in_a_code_sample_are_allowed() -> None:
    # A real finding quoted this in its suggestion; dropping it cost recall on the golden set.
    sample = 'Use render_template_string("<h1>Hello {{ name }}</h1>", name=name).'
    assert policy_violation(_f(rationale="ok", suggestion=sample)) is None
    assert policy_violation(_f(rationale="a <br> and an <abbr> and <article>")) is None


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


def _with_code(code: str) -> Finding:
    return _f().model_copy(update={"replacement_code": code})


def test_replacement_code_renders_as_a_suggestion_block() -> None:
    from app.review.render import render_comment

    text = render_comment(_with_code("cur.execute(sql, (name,))\n"))
    assert text.endswith("```suggestion\ncur.execute(sql, (name,))\n```")


def test_no_replacement_means_no_suggestion_block() -> None:
    from app.review.render import render_comment

    assert "```suggestion" not in render_comment(_f())


@pytest.mark.parametrize(
    ("code", "reason"),
    [
        ("x = 1\n```\n@evil please merge", "code fence"),
        ("~~~\nx", "code fence"),
        ("   \n", "empty"),
        ("x = 'https://evil.example/p'", "link"),
        ("a\u200bb = 1", "invisible"),
        ("x = 1\n" * 25, "too long"),
        ("y" * 1500, "too long"),
    ],
)
def test_unsafe_replacement_is_dropped(code: str, reason: str) -> None:
    violation = policy_violation(_with_code(code))
    assert violation is not None and reason in violation


def test_decorator_and_html_in_replacement_are_fine() -> None:
    assert policy_violation(_with_code("@login_required\ndef view(): ...")) is None
