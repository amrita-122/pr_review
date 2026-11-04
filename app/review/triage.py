"""Triage: decide which files deserve the expensive reviewer. Rules first, then a cheap model.

A file is `high` risk (full reviewer) or `low` risk (cheap reviewer, still reviewed). Triage is
itself an injection target ("this file is low risk" in a comment), so:
- Rules decide first and the model can never override them: a file whose diff touches a dangerous
  pattern or a sensitive path is HIGH no matter what any text in it says.
- The model only sees files the rules leave undecided, answers with ONE enum (no free text), and
  any error, timeout or odd output means HIGH (the safe side).
- Its input uses the same random per-call delimiters as the reviewers.
"""

import asyncio
import re
from collections.abc import Awaitable, Callable, Mapping
from typing import Literal

import structlog
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import BaseMessage, HumanMessage, SystemMessage
from pydantic import BaseModel

from app.diff.parse import ParsedFile
from app.review.sanitize import strip_invisible

logger = structlog.get_logger()

Risk = Literal["high", "low"]
Triager = Callable[[Mapping[str, ParsedFile]], Awaitable[dict[str, Risk]]]

# Content that forces HIGH: sinks and secrets-shaped words anywhere in the diff text.
_DANGEROUS = re.compile(
    r"\b(eval|exec|compile|subprocess|popen|pickle|marshal|yaml\.load|shell\s*=\s*True)\b"
    r"|os\.system|verify\s*=\s*False|\bexecute\w*\s*\(|\b(select|insert|update|delete)\b.+\b"
    r"(from|into|set|where)\b|render_template_string|innerHTML|dangerouslySetInnerHTML"
    r"|(password|passwd|secret|token|api[_-]?key|private[_-]?key|credential)"
    r"|\b(md5|sha1)\b|\brandom\.|allow_origins|access-control|\bcors\b"
    r"|\b(chmod|chown)\b"
    # letter-boundaries (not \b): is_admin / get_auth / user_session match, "author" does not
    r"|(?<![a-z])(jwt|oauth|login|session|admin|permission\w*"
    r"|auth(?:enticat\w*|oriz\w*|[nz])?)(?![a-z])",
    re.IGNORECASE,
)
# Paths that force HIGH.
_SENSITIVE_PATH = re.compile(
    r"(^|/)(auth\w*|login|session\w*|permission\w*|admin\w*|crypto\w*|security\w*|secrets?|"
    r"payments?|billing|db|database|sql|models?|routes?|views?|api|handlers?|middleware|"
    r"settings|config\w*|\.github|\.env.*|Dockerfile.*|docker-compose.*)(/|\.|$)",
    re.IGNORECASE,
)
# Paths that make a file LOW risk, unless the content scan above says otherwise.
_LOW_PATH = re.compile(
    r"(^|/)(tests?|docs?|examples?|fixtures?)/|\.(md|rst|txt|css|scss|svg|png|jpg|gif|ico)$"
    r"|(^|/)(README|LICENSE|CHANGELOG|CONTRIBUTING)[^/]*$",
    re.IGNORECASE,
)


def rule_risk(path: str, numbered_diff: str) -> Risk | None:
    """HIGH or LOW when the rules are sure; None when the model should decide."""
    if _DANGEROUS.search(numbered_diff) or _SENSITIVE_PATH.search(path):
        return "high"
    if _LOW_PATH.search(path):
        return "low"
    return None


class TriageResult(BaseModel):
    """What the cheap model is allowed to say: one word, nothing else."""

    risk: Risk


def make_triager(
    model: BaseChatModel,
    *,
    prompt: str,
    timeout_seconds: float = 30.0,
    max_concurrency: int = 5,
    build_message: Callable[[str, str], str],
) -> Triager:
    """The model-backed triager. `build_message(path, numbered)` wraps the untrusted data."""
    structured = model.with_structured_output(TriageResult, method="json_schema")
    semaphore = asyncio.Semaphore(max_concurrency)

    async def ask(path: str, numbered: str) -> Risk:
        messages: list[BaseMessage] = [
            SystemMessage(content=prompt),
            HumanMessage(content=build_message(path, numbered)),
        ]
        try:
            async with semaphore, asyncio.timeout(timeout_seconds):
                result = await structured.ainvoke(messages)
        except Exception as exc:  # any failure -> the safe side
            logger.warning("triage_failed", file=path, error=type(exc).__name__)
            return "high"
        if isinstance(result, TriageResult) and result.risk == "low":
            return "low"
        return "high"

    async def triage(files: Mapping[str, ParsedFile]) -> dict[str, Risk]:
        decided: dict[str, Risk] = {}
        undecided: list[str] = []
        for path, parsed in files.items():
            verdict = rule_risk(path, strip_invisible(parsed.numbered))
            if verdict is None:
                undecided.append(path)
            else:
                decided[path] = verdict
        answers = await asyncio.gather(*(ask(p, files[p].numbered) for p in undecided))
        decided.update(zip(undecided, answers, strict=True))
        logger.info(
            "triage_done",
            high=sum(r == "high" for r in decided.values()),
            low=sum(r == "low" for r in decided.values()),
            asked_model=len(undecided),
        )
        return {path: decided[path] for path in files}  # keep the input order

    return triage
