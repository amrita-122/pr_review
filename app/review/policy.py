"""Output policy: what the bot is allowed to say. Pure: no IO.

The model's text is untrusted (the PR may have steered it). A finding that breaks the policy is
DROPPED with a reason, never "cleaned": an attacker message with the link removed is still an
attacker message.

Code spans and fenced blocks are exempt from the @mention and HTML checks, because GitHub does not
notify or render inside them; a rationale like "use the `@login_required` decorator" is fine.
"""

import re

from app.review.sanitize import count_invisible
from app.review.schemas import Finding

MAX_TITLE_CHARS = 200
MAX_RATIONALE_CHARS = 1000
MAX_SUGGESTION_CHARS = 1000

_CODE = re.compile(r"```.*?```|`[^`\n]*`", re.DOTALL)
# Links, images, and script-capable URI schemes. Checked everywhere, code spans included.
_URL = re.compile(
    r"(?:https?|ftp)://|(?<![\w.])www\.|\]\(|!\[|\b(?:javascript|data|vbscript):",
    re.IGNORECASE,
)
_MENTION = re.compile(r"(?<![\w`])@[A-Za-z0-9]")
# Tags GitHub renders or that can fetch/execute something, plus HTML comments (hidden text).
# Harmless layout tags such as <h1> or <br> inside a code sample are allowed.
_HTML = re.compile(
    r"<!--|<\s*/?\s*(?:a|img|picture|source|video|audio|iframe|frame|object|embed|script|style"
    r"|link|meta|base|form|input|button|svg|details|summary)(?![A-Za-z0-9])",
    re.IGNORECASE,
)


def _fields(finding: Finding) -> list[tuple[str, str, int]]:
    return [
        ("title", finding.title, MAX_TITLE_CHARS),
        ("rationale", finding.rationale, MAX_RATIONALE_CHARS),
        ("suggestion", finding.suggestion or "", MAX_SUGGESTION_CHARS),
    ]


def policy_violation(finding: Finding) -> str | None:
    """The reason this finding may not be posted, or None if it passes."""
    for name, text, limit in _fields(finding):
        if len(text) > limit:
            return f"output policy: {name} too long"
        if count_invisible(text):
            return "output policy: invisible characters"
        if _URL.search(text):
            return "output policy: link"
        outside_code = _CODE.sub("", text)
        if _MENTION.search(outside_code):
            return "output policy: mention"
        if _HTML.search(outside_code):
            return "output policy: html"
    return None
