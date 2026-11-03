"""Turn findings into review text. Pure: no GitHub or HTTP."""

from collections import Counter

from app.review.schemas import Finding

MAX_SKIPPED_LISTED = 20
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
    reviewed_count: int, findings: list[Finding], skipped: list[tuple[str, str]]
) -> str:
    lines = [f"Reviewed {reviewed_count} file(s)."]
    if findings:
        counts = Counter(f.severity.value for f in findings)
        detail = ", ".join(f"{n} {sev}" for sev, n in counts.items())
        lines.append(f"{len(findings)} finding(s): {detail}. See the inline comments.")
    else:
        lines.append("No findings.")
    if skipped:
        lines += ["", f"Skipped {len(skipped)} file(s):"]
        lines += [f"- `{_inline(name)}`: {reason}" for name, reason in skipped[:MAX_SKIPPED_LISTED]]
        if len(skipped) > MAX_SKIPPED_LISTED:
            lines.append(f"- ...and {len(skipped) - MAX_SKIPPED_LISTED} more")
    return "\n".join(lines)
