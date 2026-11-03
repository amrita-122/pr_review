from collections.abc import Iterable, Mapping

import structlog

from app.diff.parse import ParsedFile
from app.review.schemas import Finding

logger = structlog.get_logger()

Dropped = tuple[Finding, str]  # (finding, reason)


def partition_findings(
    findings: Iterable[Finding], reviewed: Mapping[str, ParsedFile]
) -> tuple[list[Finding], list[Dropped]]:
    """Split findings into (kept, dropped-with-reason).

    Kept = file was reviewed and the line is one GitHub can attach a comment to.
    Every drop is logged with its reason. Phase 5 adds the output policy here.
    """
    kept: list[Finding] = []
    dropped: list[Dropped] = []
    for finding in findings:
        parsed = reviewed.get(finding.file_path)
        if parsed is None:
            reason = "file not reviewed"
        elif finding.line not in parsed.commentable_lines:
            reason = "line not commentable"
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
