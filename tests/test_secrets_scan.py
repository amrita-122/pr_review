"""Secret-scanning pre-pass (10.32)."""

import pytest

from app.diff.parse import parse_patch
from app.review.graph import ReviewLimits, review_diff
from app.review.schemas import Category, Finding, Severity
from app.review.secrets_scan import entropy, scan_secrets


def _parsed(*added: str, path: str = "cfg.py"):  # type: ignore[no-untyped-def]
    body = "".join(f"+{line}\n" for line in added)
    return {path: parse_patch(path, f"@@ -0,0 +1,{len(added)} @@\n{body}")}


# Built at runtime so no real-looking secret sits in this file (secret scanners read repos too).
AWS = "AK" + "IA" + "ABCDEFGHIJKLMNOP"
GH = "ghp_" + "a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q7R8"
SLACK = "xoxb-" + "1234567890-abcdefghij"
PEM = "-----BEGIN RSA " + "PRIVATE KEY-----"


@pytest.mark.parametrize(
    ("line", "kind"),
    [
        (f'KEY = "{AWS}"', "AWS access key id"),
        (f"token: {GH}", "GitHub token"),
        (f"SLACK={SLACK}", "Slack token"),
        (PEM, "private key block"),
        ('api_key = "q8Zx3Lm9Vb2Nc7Rt5Yw1Hp4Kd6Fg0Js"', "high-entropy credential value"),
    ],
)
def test_detects_secrets_on_added_lines(line: str, kind: str) -> None:
    (finding,) = scan_secrets(_parsed("import os", line))
    assert finding.line == 2 and finding.category is Category.SECRETS and kind in finding.title
    assert finding.agent.value == "security"


def test_finding_text_never_contains_the_secret() -> None:
    (finding,) = scan_secrets(_parsed(f'KEY = "{AWS}"'))
    text = " ".join([finding.title, finding.rationale, finding.suggestion or ""])
    assert AWS not in text and AWS[:6] not in text


@pytest.mark.parametrize(
    "line",
    [
        'password = "changeme-changeme-change"',
        'api_key = "your-api-key-goes-here-123"',
        'token = os.environ["GITHUB_TOKEN_VALUE"]',
        'secret = "aaaaaaaaaaaaaaaaaaaaaaaa"',  # long but no entropy
        'api_key = "${API_KEY_FROM_VAULT_123}"',
        "def get_token(user): ...",
        'title = "the quick brown fox jumps over"',
    ],
)
def test_placeholders_and_normal_code_are_not_flagged(line: str) -> None:
    assert scan_secrets(_parsed(line)) == []


def test_removed_and_context_lines_are_ignored() -> None:
    patch = f'@@ -1,2 +1,1 @@\n-KEY = "{AWS}"\n ok = 1\n'
    assert scan_secrets({"a.py": parse_patch("a.py", patch)}) == []
    patch = f'@@ -1,1 +1,2 @@\n KEY = "{AWS}"\n+ok = 2\n'
    assert scan_secrets({"a.py": parse_patch("a.py", patch)}) == []


def test_entropy_orders_random_above_repetitive() -> None:
    assert entropy("aaaaaaaa") == 0 and entropy("q8Zx3Lm9Vb2Nc7Rt") > 3.5


async def _no_model(path: str, numbered: str) -> list[Finding]:
    return []


async def test_scan_runs_inside_review_diff_only_when_enabled() -> None:
    files = _parsed(f'KEY = "{AWS}"')
    off = await review_diff(files, _no_model, ReviewLimits(10, 10_000))
    on = await review_diff(files, _no_model, ReviewLimits(10, 10_000, secret_scan=True))
    assert off.findings == [] and len(on.findings) == 1


async def test_public_repo_secret_goes_to_the_private_channel_not_the_pr() -> None:
    files = _parsed(f'KEY = "{AWS}"')
    outcome = await review_diff(
        files, _no_model, ReviewLimits(10, 10_000, secret_scan=True), public=True
    )
    assert outcome.findings == [] and len(outcome.private) == 1
    assert outcome.private[0].severity is Severity.HIGH
