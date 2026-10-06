"""Fake reviewer for Phase 2 and for tests. Same signature the LLM reviewer will have."""

import re

from app.review.schemas import Category, Finding, Severity

_ADDED_LINE = re.compile(r"^L(\d+) \+ (.*)$")
_TODO = re.compile(r"TODO")
_PASSWORD = re.compile(r"password\s*=", re.IGNORECASE)


def review_file(path: str, numbered_diff: str) -> list[Finding]:
    """One file's numbered diff (see app.diff.parse) -> findings. Looks at added lines only."""
    findings: list[Finding] = []
    for raw in numbered_diff.splitlines():
        match = _ADDED_LINE.match(raw)
        if match is None:
            continue
        line, code = int(match.group(1)), match.group(2)
        if _PASSWORD.search(code):
            findings.append(
                Finding(
                    file_path=path,
                    line=line,
                    severity=Severity.HIGH,
                    category=Category.SECRETS,
                    title="Possible hardcoded password",
                    rationale="A password appears to be assigned in source code.",
                    suggestion="Read it from an environment variable or a secret store.",
                )
            )
        if _TODO.search(code):
            findings.append(
                Finding(
                    file_path=path,
                    line=line,
                    severity=Severity.LOW,
                    category=Category.OTHER,
                    title="TODO left in code",
                    rationale="A TODO was added in this change.",
                )
            )
    return findings
