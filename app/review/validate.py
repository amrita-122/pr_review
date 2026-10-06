import logging
from collections.abc import Iterable, Mapping

from app.diff.parse import ParsedFile
from app.review.schemas import Finding

logger = logging.getLogger(__name__)


def validate_findings(
    findings: Iterable[Finding], reviewed: Mapping[str, ParsedFile]
) -> list[Finding]:
    """Keep only findings whose file was reviewed and whose line GitHub can attach a comment to.

    Dropped findings are logged with a reason. Phase 5 adds the output policy here.
    """
    kept: list[Finding] = []
    for finding in findings:
        parsed = reviewed.get(finding.file_path)
        if parsed is None:
            reason = "file not reviewed"
        elif finding.line not in parsed.commentable_lines:
            reason = "line not commentable"
        else:
            kept.append(finding)
            continue
        logger.warning(
            "dropped finding: %s (file=%r line=%d category=%s)",
            reason,
            finding.file_path,
            finding.line,
            finding.category,
        )
    return kept
