"""Do not repeat yourself: a finding already commented on an earlier push is not posted again.

Pure: the caller (which owns the database) passes in what was posted before.
"""

from collections.abc import Iterable
from dataclasses import dataclass

from app.review.schemas import Finding
from app.review.validate import Dropped

ALREADY_POSTED = "already posted on an earlier push"
LINE_TOLERANCE = 5  # lines shift when code above is edited; the eval matcher allows 3


@dataclass(frozen=True)
class KnownFinding:
    """A finding of an earlier run of the same PR that is (or was) visible on it."""

    file: str
    category: str
    line: int


def is_repeat(finding: Finding, known: Iterable[KnownFinding]) -> bool:
    return any(
        k.file == finding.file_path
        and k.category == finding.category.value
        and abs(k.line - finding.line) <= LINE_TOLERANCE
        for k in known
    )


def drop_repeats(
    findings: list[Finding], known: Iterable[KnownFinding]
) -> tuple[list[Finding], list[Dropped]]:
    """Split into (new findings, repeats with the reason)."""
    known = list(known)
    if not known:
        return findings, []
    fresh: list[Finding] = []
    repeats: list[Dropped] = []
    for finding in findings:
        if is_repeat(finding, known):
            repeats.append((finding, ALREADY_POSTED))
        else:
            fresh.append(finding)
    return fresh, repeats
