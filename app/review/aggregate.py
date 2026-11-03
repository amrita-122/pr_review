"""Merge overlapping findings and cap what one review says. Pure: no IO, no model.

Input is the validator's output: findings that are individually safe to post. Two agents can
report the same problem, or one agent can report it twice on neighbouring lines; the author should
see it once, at the highest severity.
"""

from app.review.schemas import Agent, Finding, ReviewFinding, Severity
from app.review.validate import Dropped

MERGE_DISTANCE = 2  # same file + same category within this many lines = one problem

SEVERITY_RANK = {Severity.CRITICAL: 0, Severity.HIGH: 1, Severity.MEDIUM: 2, Severity.LOW: 3}
MERGED = "merged into a duplicate finding"
OVER_CAP = "over comment cap"


def _agent_rank(finding: Finding) -> int:
    # On equal severity the security finding wins a merge.
    agent = finding.agent if isinstance(finding, ReviewFinding) else Agent.SECURITY
    return 0 if agent is Agent.SECURITY else 1


def _agent(finding: Finding) -> Agent:
    return finding.agent if isinstance(finding, ReviewFinding) else Agent.SECURITY


def _same_problem(a: Finding, b: Finding) -> bool:
    if a.file_path != b.file_path:
        return False
    if a.category == b.category and abs(a.line - b.line) <= MERGE_DISTANCE:
        return True
    # Two different agents pointing at the exact same line are talking about the same code.
    return a.line == b.line and _agent(a) is not _agent(b)


def sort_key(finding: Finding) -> tuple[int, int, str, int]:
    return (SEVERITY_RANK[finding.severity], _agent_rank(finding), finding.file_path, finding.line)


def aggregate(findings: list[Finding], max_comments: int) -> tuple[list[Finding], list[Dropped]]:
    """-> (findings to post, most severe first; dropped with reason)."""
    kept: list[Finding] = []
    dropped: list[Dropped] = []
    for finding in sorted(findings, key=sort_key):
        if any(_same_problem(finding, existing) for existing in kept):
            dropped.append((finding, MERGED))
        else:
            kept.append(finding)
    if len(kept) > max_comments:
        dropped += [(f, OVER_CAP) for f in kept[max_comments:]]
        kept = kept[:max_comments]
    return kept, dropped
