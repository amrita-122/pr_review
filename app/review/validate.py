from collections.abc import Iterable, Mapping

import structlog

from app.diff.parse import ParsedFile
from app.review.policy import policy_violation
from app.review.schemas import AGENT_CATEGORIES, Finding, ReviewFinding

logger = structlog.get_logger()

Dropped = tuple[Finding, str]  # (finding, reason)
MAX_RANGE_LINES = 20


def _range_problem(finding: Finding, parsed: ParsedFile) -> str | None:
    """Why this finding's line (or range) cannot carry a GitHub comment, or None if it can.

    A range must lie entirely on one side and on lines that are in the diff: a gap between hunks
    is not in the set, so a range can never span one (GitHub rejects those with a 422)."""
    allowed = parsed.removed_lines if finding.side == "LEFT" else parsed.commentable_lines
    start = finding.start_line if finding.start_line is not None else finding.line
    if start > finding.line:
        return "range start after end"
    if finding.line - start + 1 > MAX_RANGE_LINES:
        return "range too long"
    if all(n in allowed for n in range(start, finding.line + 1)):
        return None
    return "line not commentable"


def partition_findings(
    findings: Iterable[Finding], reviewed: Mapping[str, ParsedFile]
) -> tuple[list[Finding], list[Dropped]]:
    """Split findings into (kept, dropped-with-reason).

    Kept = file was reviewed, the line is one GitHub can attach a comment to, the category
    belongs to the agent that produced it, and the text passes the output policy (no links,
    mentions, HTML, hidden characters, or runaway length).
    Every drop is logged with its reason; the text itself is never logged.
    """
    kept: list[Finding] = []
    dropped: list[Dropped] = []
    for finding in findings:
        parsed = reviewed.get(finding.file_path)
        if parsed is None:
            reason = "file not reviewed"
        elif (problem := _range_problem(finding, parsed)) is not None:
            reason = problem
        elif (
            isinstance(finding, ReviewFinding)
            and finding.category not in AGENT_CATEGORIES[finding.agent]
        ):
            reason = "category not allowed for agent"
        elif (violation := policy_violation(finding)) is not None:
            reason = violation
        else:
            kept.append(finding)
            continue
        dropped.append((finding, reason))
        logger.warning(
            "finding_dropped",
            reason=reason,
            file=finding.file_path,
            line=finding.line,
            category=finding.category.value,
        )
    return kept, dropped


def validate_findings(
    findings: Iterable[Finding], reviewed: Mapping[str, ParsedFile]
) -> list[Finding]:
    return partition_findings(findings, reviewed)[0]
