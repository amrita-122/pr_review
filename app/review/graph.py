"""The review graph. Pure core: files in, validated findings out. No GitHub or HTTP imports."""

import logging
import operator
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Annotated, Any, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.types import Send

from app.diff.parse import ParsedFile
from app.review.llm import Reviewer
from app.review.schemas import Finding
from app.review.validate import validate_findings

logger = logging.getLogger(__name__)

MAX_CONCURRENT_FILES = 5  # parallel model calls; keeps us under provider rate limits


@dataclass(frozen=True)
class ReviewLimits:
    max_files: int
    max_chars_per_file: int


@dataclass
class ReviewOutcome:
    findings: list[Finding]  # validated: safe to post
    reviewed: dict[str, ParsedFile]  # files the model actually reviewed
    skipped: list[tuple[str, str]] = field(default_factory=list)  # (path, reason)


class ReviewState(TypedDict):
    files: dict[str, ParsedFile]  # input: everything we were handed
    to_review: dict[str, ParsedFile]  # after limits
    limit_skipped: list[tuple[str, str]]
    findings: Annotated[list[Finding], operator.add]  # reducer: parallel branches append
    errors: Annotated[list[tuple[str, str]], operator.add]  # (path, reason) for failed files
    validated: list[Finding]


class FileTask(TypedDict):
    path: str
    numbered: str


def _prepare(limits: ReviewLimits) -> Any:  # node callable; langgraph's typing is too strict
    def prepare(state: ReviewState) -> dict[str, Any]:
        to_review: dict[str, ParsedFile] = {}
        skipped: list[tuple[str, str]] = []
        for path, parsed in state["files"].items():
            if len(to_review) >= limits.max_files:
                skipped.append((path, f"over the {limits.max_files}-file limit"))
            elif len(parsed.numbered) > limits.max_chars_per_file:
                skipped.append((path, f"diff too large ({len(parsed.numbered)} chars)"))
            else:
                to_review[path] = parsed
        return {"to_review": to_review, "limit_skipped": skipped}

    return prepare


def _fan_out(state: ReviewState) -> list[Send] | str:
    """One `Send` per file: LangGraph runs these review_file calls in parallel."""
    if not state["to_review"]:
        return "validate"
    return [
        Send("review_file", FileTask(path=path, numbered=parsed.numbered))
        for path, parsed in state["to_review"].items()
    ]


def _review_file(reviewer: Reviewer) -> Any:  # node callable; takes a FileTask, not the state
    async def review_file(task: FileTask) -> dict[str, Any]:
        try:
            findings = await reviewer(task["path"], task["numbered"])
        except Exception as exc:  # one bad file must never fail the whole run
            logger.warning("review failed for %r: %s: %s", task["path"], type(exc).__name__, exc)
            return {"errors": [(task["path"], "review failed")]}
        return {"findings": findings}

    return review_file


def _validate(state: ReviewState) -> dict[str, Any]:
    return {"validated": validate_findings(state["findings"], state["to_review"])}


def build_graph(reviewer: Reviewer, limits: ReviewLimits) -> Any:
    graph = StateGraph(ReviewState)
    graph.add_node("prepare", _prepare(limits))
    graph.add_node("review_file", _review_file(reviewer))
    graph.add_node("validate", _validate)
    graph.add_edge(START, "prepare")
    graph.add_conditional_edges("prepare", _fan_out, ["review_file", "validate"])
    graph.add_edge("review_file", "validate")
    graph.add_edge("validate", END)
    return graph.compile()


async def review_diff(
    files: Mapping[str, ParsedFile], reviewer: Reviewer, limits: ReviewLimits
) -> ReviewOutcome:
    """Review parsed files. Evals call this directly; it knows nothing about GitHub."""
    app = build_graph(reviewer, limits)
    initial: ReviewState = {
        "files": dict(files),
        "to_review": {},
        "limit_skipped": [],
        "findings": [],
        "errors": [],
        "validated": [],
    }
    final = await app.ainvoke(initial, config={"max_concurrency": MAX_CONCURRENT_FILES})
    failed = {path for path, _ in final["errors"]}
    reviewed = {p: f for p, f in final["to_review"].items() if p not in failed}
    return ReviewOutcome(
        findings=final["validated"],
        reviewed=reviewed,
        skipped=final["limit_skipped"] + final["errors"],
    )
