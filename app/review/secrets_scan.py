"""Secret-scanning pre-pass (10.32): regex for well-known key formats plus an entropy check on
credential-looking assignments. Pure and deterministic: no model, no IO.

It reads only ADDED lines of the numbered diff ("L42 + code"). It never puts any part of the
matched value into a finding: the text names the KIND of secret and nothing else, so the finding
itself can never leak what it found. Findings go through the same validator, comment cap and
public-repo routing (ADR 0008) as model findings, tagged as the security agent.
"""

import math
import re
from collections import Counter
from collections.abc import Mapping

from app.diff.parse import ParsedFile
from app.review.schemas import Agent, Category, ReviewFinding, Severity

_ADDED = re.compile(r"^L(\d+) \+ (.*)$")

# (kind, regex). Anchored on each provider's documented prefix and length, so false alarms are rare.
_KNOWN: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("AWS access key id", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("GitHub token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}\b")),
    ("GitHub fine-grained token", re.compile(r"\bgithub_pat_[A-Za-z0-9_]{50,}\b")),
    ("Slack token", re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}")),
    ("private key block", re.compile(r"-----BEGIN (?:[A-Z]+ )?PRIVATE KEY(?: BLOCK)?-----")),
    ("Stripe live key", re.compile(r"\b[sr]k_live_[0-9A-Za-z]{24,}\b")),
    ("Anthropic API key", re.compile(r"\bsk-ant-[A-Za-z0-9_-]{20,}")),
    ("OpenAI-style API key", re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9]{32,}\b")),
    ("Google API key", re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b")),
)
# name = "value": a credential-sounding name with a long quoted value.
_ASSIGNMENT = re.compile(
    r"(?i)\b\w*(?:secret|token|passw(?:or)?d|api[_-]?key|private[_-]?key|credential)\w*"
    r"""\s*[:=]\s*["']([^"'\s]{16,})["']"""
)
_PLACEHOLDER = re.compile(
    r"(?i)example|your|changeme|placeholder|dummy|sample|xxxx|\*{4}|<|\$\{|\{\{|%\(|test"
)
MIN_ENTROPY = 3.5  # bits per character; English words and repeated text score well below this
MAX_FINDINGS_PER_FILE = 10


def entropy(value: str) -> float:
    counts = Counter(value)
    total = len(value)
    return -sum(n / total * math.log2(n / total) for n in counts.values())


def _hit(code: str) -> tuple[str, Severity] | None:
    for kind, pattern in _KNOWN:
        if pattern.search(code):
            return kind, Severity.HIGH
    match = _ASSIGNMENT.search(code)
    if match:
        value = match.group(1)
        if not _PLACEHOLDER.search(value) and entropy(value) >= MIN_ENTROPY:
            return "high-entropy credential value", Severity.MEDIUM
    return None


def scan_file(path: str, parsed: ParsedFile) -> list[ReviewFinding]:
    findings: list[ReviewFinding] = []
    for raw in parsed.numbered.splitlines():
        added = _ADDED.match(raw)
        if added is None:
            continue
        hit = _hit(added.group(2))
        if hit is None:
            continue
        kind, severity = hit
        findings.append(
            ReviewFinding(
                file_path=path,
                line=int(added.group(1)),
                severity=severity,
                category=Category.SECRETS,
                title=f"Possible hard-coded secret ({kind})",
                rationale=(
                    f"This added line looks like it contains a {kind}. Anything committed stays "
                    "in the git history even if it is removed later, so treat it as exposed."
                ),
                suggestion=(
                    "Rotate the credential, then load it from an environment variable or a "
                    "secret store instead of the source."
                ),
                agent=Agent.SECURITY,
            )
        )
        if len(findings) >= MAX_FINDINGS_PER_FILE:
            break
    return findings


def scan_secrets(files: Mapping[str, ParsedFile]) -> list[ReviewFinding]:
    return [finding for path, parsed in files.items() for finding in scan_file(path, parsed)]
