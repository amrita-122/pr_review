"""The review graph. Pure core: files in, validated findings out. No GitHub or HTTP imports."""

import operator
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Annotated, Any, TypedDict

import structlog
from langchain_core.callbacks import BaseCallbackHandler
from langgraph.graph import END, START, StateGraph
from langgraph.types import Send

from app.config import Settings
from app.diff.parse import ParsedFile
from app.review.aggregate import OVER_CAP, SEVERITY_RANK, aggregate
from app.review.llm import Reviewer
from app.review.schemas import Agent, Finding, ReviewFinding
from app.review.validate import Dropped, partition_findings

logger = structlog.get_logger()

MAX_CONCURRENT_FILES = 5  # parallel model calls; keeps us under provider rate limits


@dataclass(frozen=True)
class ReviewLimits:
    """Caps on what one PR can make us spend or say (the PR author controls its size)."""

    max_files: int
    max_chars_per_file: int
    max_total_chars: int = 150_000  # all reviewed diffs together
    max_findings_per_file: int = 10  # what we accept from one model call
    max_comments: int = 25  # inline comments in one review

    @classmethod
    def from_settings(cls, settings: Settings) -> "ReviewLimits":
        return cls(
            max_files=settings.max_files_per_pr,
            max_chars_per_file=settings.max_chars_per_file,
            max_total_chars=settings.max_total_chars_per_pr,
            max_findings_per_file=settings.max_findings_per_file,
            max_comments=settings.max_comments_per_review,
        )


@dataclass
class ReviewOutcome:
    findings: list[Finding]  # validated: safe to post
    reviewed: dict[str, ParsedFile]  # files the model actually reviewed
    skipped: list[tuple[str, str]] = field(default_factory=list)  # (path, reason)
    dropped: list[Dropped] = field(default_factory=list)  # validator drops, with reason
    raw_count: int = 0  # findings the model produced, before validation


class ReviewState(TypedDict):
    files: dict[str, ParsedFile]  # input: everything we were handed
    to_review: dict[str, ParsedFile]  # after limits
    limit_skipped: list[tuple[str, str]]
    findings: Annotated[list[Finding], operator.add]  # reducer: parallel branches append
    errors: Annotated[list[tuple[str, str]], operator.add]  # (path, reason) for failed files
    validated: list[Finding]
    dropped: list[Dropped]


class FileTask(TypedDict):
    path: str
    numbered: str
    agent: str  # which reviewer runs on this file: "security" or "quality"


def _prepare(limits: ReviewLimits) -> Any:  # node callable; langgraph's typing is too strict
    def prepare(state: ReviewState) -> dict[str, Any]:
        to_review: dict[str, ParsedFile] = {}
        skipped: list[tuple[str, str]] = []
        total = 0
        for path, parsed in state["files"].items():
            size = len(parsed.numbered)
            if len(to_review) >= limits.max_files:
                skipped.append((path, f"over the {limits.max_files}-file limit"))
            elif size > limits.max_chars_per_file:
                skipped.append((path, f"diff too large ({size} chars)"))
            elif total + size > limits.max_total_chars:
                skipped.append((path, "over the total PR size limit"))
            else:
                to_review[path] = parsed
                total += size
        return {"to_review": to_review, "limit_skipped": skipped}

    return prepare


def _fan_out(agents: list[str]) -> Any:
    def fan_out(state: ReviewState) -> list[Send] | str:
        """One `Send` per (file, agent): LangGraph runs these review_file calls in parallel."""
        if not state["to_review"]:
            return "validate"
        return [
            Send("review_file", FileTask(path=path, numbered=parsed.numbered, agent=agent))
            for path, parsed in state["to_review"].items()
            for agent in agents
        ]

    return fan_out


def _review_file(
    reviewers: Mapping[str, Reviewer], limits: ReviewLimits
) -> Any:  # node callable; takes a FileTask, not the state
    async def review_file(task: FileTask) -> dict[str, Any]:
        agent = task["agent"]
        try:
            findings = await reviewers[agent](task["path"], task["numbered"])
        except Exception as exc:  # one bad file must never fail the whole run
            logger.warning(
                "file_review_failed", file=task["path"], agent=agent, error=type(exc).__name__
            )
            return {"errors": [(task["path"], f"{agent} review failed")]}
        if len(findings) > limits.max_findings_per_file:
            logger.warning(
                "findings_truncated",
                file=task["path"],
                agent=agent,
                produced=len(findings),
                kept=limits.max_findings_per_file,
            )
            findings = sorted(findings, key=lambda f: SEVERITY_RANK[f.severity])[
                : limits.max_findings_per_file
            ]
        # The agent comes from OUR node, never from the model's output.
        tagged = [ReviewFinding(**f.model_dump(), agent=Agent(agent)) for f in findings]
        return {"findings": tagged}

    return review_file


def _validate(limits: ReviewLimits) -> Any:
    def validate(state: ReviewState) -> dict[str, Any]:
        kept, dropped = partition_findings(state["findings"], state["to_review"])
        kept, merged = aggregate(kept, limits.max_comments)
        if any(reason == OVER_CAP for _, reason in merged):
            logger.warning(
                "comment_cap_hit",
                kept=len(kept),
                dropped=sum(reason == OVER_CAP for _, reason in merged),
            )
        return {"validated": kept, "dropped": dropped + merged}

    return validate


def build_graph(reviewers: Mapping[str, Reviewer], limits: ReviewLimits) -> Any:
    graph = StateGraph(ReviewState)
    graph.add_node("prepare", _prepare(limits))
    graph.add_node("review_file", _review_file(reviewers, limits))
    graph.add_node("validate", _validate(limits))
    graph.add_edge(START, "prepare")
    graph.add_conditional_edges("prepare", _fan_out(list(reviewers)), ["review_file", "validate"])
    graph.add_edge("review_file", "validate")
    graph.add_edge("validate", END)
    return graph.compile()


async def review_diff(
    files: Mapping[str, ParsedFile],
    reviewers: Reviewer | Mapping[str, Reviewer],
    limits: ReviewLimits,
    *,
    run_name: str | None = None,
    metadata: Mapping[str, str] | None = None,
    callbacks: list[BaseCallbackHandler] | None = None,
) -> ReviewOutcome:
    """Review parsed files. Evals call this directly; it knows nothing about GitHub.

    `reviewers` is one reviewer (treated as the security agent) or {agent name: reviewer}; every
    file goes to every agent, in parallel. run_name / metadata / callbacks only label and observe
    the run (tracing, token counting).
    """
    by_agent = {Agent.SECURITY.value: reviewers} if callable(reviewers) else dict(reviewers)
    app = build_graph(by_agent, limits)
    initial: ReviewState = {
        "files": dict(files),
        "to_review": {},
        "limit_skipped": [],
        "findings": [],
        "errors": [],
        "validated": [],
        "dropped": [],
    }
    config: dict[str, Any] = {"max_concurrency": MAX_CONCURRENT_FILES}
    if run_name:
        config["run_name"] = run_name
    if metadata:
        config["metadata"] = dict(metadata)
    if callbacks:
        config["callbacks"] = callbacks
    final = await app.ainvoke(initial, config=config)
    failed = {
        path for path, _ in final["errors"]
    }  # any agent failing marks the file not fully reviewed
    reviewed = {p: f for p, f in final["to_review"].items() if p not in failed}
    return ReviewOutcome(
        findings=final["validated"],
        reviewed=reviewed,
        skipped=final["limit_skipped"] + final["errors"],
        dropped=final["dropped"],
        raw_count=len(final["findings"]),
    )
