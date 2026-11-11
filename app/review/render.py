"""Turn findings into review text. Pure: no GitHub or HTTP."""

from collections import Counter

from app.review.schemas import Finding

MAX_SKIPPED_LISTED = 20
INLINE_NOTE = " See the inline comments."
MAX_REVIEW_BODY_CHARS = 60_000  # GitHub's limit is 65,536
MAX_NAME_CHARS = 100


def _inline(text: str) -> str:
    """File names are untrusted: flatten whitespace and neutralise backticks before quoting."""
    flat = " ".join(text.replace("`", "'").split())
    return flat if len(flat) <= MAX_NAME_CHARS else flat[:MAX_NAME_CHARS] + "..."


def render_comment(finding: Finding) -> str:
    parts = [f"**[{finding.severity.value.upper()}] {finding.title}**", "", finding.rationale]
    if finding.suggestion:
        parts += ["", f"Suggestion: {finding.suggestion}"]
    return "\n".join(parts)


def render_summary(
    reviewed_count: int,
    findings: list[Finding],
    skipped: list[tuple[str, str]],
    *,
    overflow: int = 0,
    merged: int = 0,
    failed: int = 0,
    held: int = 0,
) -> str:
    """The review's top-level text. `overflow` = findings cut by the comment cap, `merged` =
    duplicates folded into another finding; both are stated so the list is never silently short.
    `failed` = files whose review step failed: a run with failures is never called clean."""
    lines = [f"Reviewed {reviewed_count} file(s)."]
    if findings:
        severities = Counter(f.severity.value for f in findings)
        detail = ", ".join(f"{n} {sev}" for sev, n in severities.items())
        lines.append(f"{len(findings)} finding(s): {detail}.{INLINE_NOTE}")
        categories = Counter(f.category.value for f in findings)
        if len(categories) > 1:
            lines.append(
                "By category: " + ", ".join(f"{n} {cat}" for cat, n in categories.most_common())
            )
    elif held and not failed and reviewed_count:
        lines.append("No other findings.")
    elif reviewed_count == 0:
        lines.append("No files were reviewed, so there is nothing to report.")
    elif failed:
        lines.append("No findings in the files that were reviewed.")
    else:
        lines.append("No findings.")
    if held:
        lines.append(f"{held} more finding(s) are pending a maintainer's confirmation.")
    if failed:
        lines.append(
            f"{failed} file(s) could NOT be reviewed because a review step failed. "
            "This is not a clean result for those files."
        )
    if overflow:
        lines.append(
            f"{overflow} lower-priority finding(s) were not posted (comment limit reached)."
        )
    if merged:
        lines.append(f"{merged} duplicate finding(s) were merged into the ones above.")
    if skipped:
        lines += ["", f"Skipped {len(skipped)} file(s):"]
        lines += [f"- `{_inline(name)}`: {reason}" for name, reason in skipped[:MAX_SKIPPED_LISTED]]
        if len(skipped) > MAX_SKIPPED_LISTED:
            lines.append(f"- ...and {len(skipped) - MAX_SKIPPED_LISTED} more")
    return "\n".join(lines)


def render_fallback_summary(summary: str, comments: list[tuple[str, int, str]]) -> str:
    """The summary for a review whose inline comments GitHub rejected: it must not point at
    inline comments that do not exist, so it says so and lists the findings in the text itself.

    `comments` = (path, line, body). Paths are untrusted: flattened like every file name.
    """
    text = summary.replace(INLINE_NOTE, "")
    if not comments:
        return text
    parts = [
        text,
        "",
        f"GitHub could not attach {len(comments)} comment(s) inline, so they are listed here:",
    ]
    parts += [f"\n**`{_inline(path)}` line {line}**\n{body}" for path, line, body in comments]
    return "\n".join(parts)[:MAX_REVIEW_BODY_CHARS]


def render_confirmed_summary(findings: list[Finding]) -> str:
    """Summary of the follow-up review posted after a maintainer's /confirm."""
    severities = Counter(f.severity.value for f in findings)
    detail = ", ".join(f"{n} {sev}" for sev, n in severities.items())
    return f"{len(findings)} finding(s) confirmed by a maintainer: {detail}.{INLINE_NOTE}"
