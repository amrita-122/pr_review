"""The review graph. Pure core: files in, validated findings out. No GitHub or HTTP imports."""

import operator
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Annotated, Any, TypedDict

import structlog
from langchain_core.callbacks import BaseCallbackHandler
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, Send, interrupt

from app.config import Settings
from app.diff.parse import ParsedFile
from app.review.aggregate import OVER_CAP, SEVERITY_RANK, aggregate
from app.review.dedupe import KnownFinding, drop_repeats
from app.review.llm import Reviewer, reviewer_key
from app.review.schemas import Agent, Finding, ReviewFinding, Severity
from app.review.triage import Risk, Triager, rule_risk
from app.review.validate import Dropped, partition_findings
from app.review.visibility import divert

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
    failed: list[str] = field(default_factory=list)  # files whose review step failed (unique)
    dropped: list[Dropped] = field(default_factory=list)  # validator drops, with reason
    raw_count: int = 0  # findings the model produced, before validation
    pending: int = 0  # held findings waiting for a maintainer. >0 means: do NOT post those
    held: list[Finding] = field(default_factory=list)  # the held findings themselves (in memory)
    private: list[Finding] = field(default_factory=list)  # public repo: for the private channel
    risk: dict[str, str] = field(
        default_factory=dict
    )  # triage verdict per file (empty = no triage)


class ReviewState(TypedDict):
    files: dict[str, ParsedFile]  # input: everything we were handed
    to_review: dict[str, ParsedFile]  # after limits
    risk: dict[str, str]  # triage: path -> "high" | "low" (empty when triage is off)
    limit_skipped: list[tuple[str, str]]
    findings: Annotated[list[Finding], operator.add]  # reducer: parallel branches append
    errors: Annotated[list[tuple[str, str]], operator.add]  # (path, reason) for failed files
    validated: list[Finding]
    private: list[Finding]  # public repo: security findings that must never be PR text
    dropped: list[Dropped]
    pre_skipped: list[tuple[str, str]]  # files skipped before the graph ran (lockfiles, binaries)


class FileTask(TypedDict):
    path: str
    numbered: str
    agent: str  # which reviewer runs on this file: "security" or "quality"
    tier: str  # "high" (full reviewer) or "low" (cheap reviewer); from triage, default "high"


def _prepare(limits: ReviewLimits) -> Any:  # node callable; langgraph's typing is too strict
    def prepare(state: ReviewState) -> dict[str, Any]:
        to_review: dict[str, ParsedFile] = {}
        skipped: list[tuple[str, str]] = []
        total = 0
        # Risk first, API order second (the sort is stable). The caps below cut from the END, so
        # padding a PR with big harmless files cannot push the risky file out of the review.
        rank = {"high": 0, None: 1, "low": 2}
        ordered = sorted(
            state["files"].items(), key=lambda item: rank[rule_risk(item[0], item[1].numbered)]
        )
        for path, parsed in ordered:
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


def _triage(triager: Triager | None) -> Any:
    async def triage(state: ReviewState) -> dict[str, Any]:
        if triager is None or not state["to_review"]:
            return {"risk": {}}
        try:
            risk: dict[str, Risk] = await triager(state["to_review"])
        except Exception as exc:  # triage is an optimisation: if it breaks, review everything fully
            logger.warning("triage_failed", error=type(exc).__name__)
            return {"risk": {}}
        return {"risk": dict(risk)}

    return triage


def _fan_out(agents: list[str]) -> Any:
    def fan_out(state: ReviewState) -> list[Send] | str:
        """One `Send` per (file, agent): LangGraph runs these review_file calls in parallel."""
        if not state["to_review"]:
            return "validate"
        return [
            Send(
                "review_file",
                FileTask(
                    path=path,
                    numbered=parsed.numbered,
                    agent=agent,
                    tier=state["risk"].get(path, "high"),  # no verdict = the full reviewer
                ),
            )
            for path, parsed in state["to_review"].items()
            for agent in agents
        ]

    return fan_out


def _review_file(
    reviewers: Mapping[str, Reviewer], limits: ReviewLimits
) -> Any:  # node callable; takes a FileTask, not the state
    async def review_file(task: FileTask) -> dict[str, Any]:
        agent = task["agent"]
        # Cheap reviewer for low-risk files when one is configured; otherwise the full one.
        reviewer = reviewers.get(reviewer_key(agent, task["tier"])) or reviewers[agent]
        try:
            findings = await reviewer(task["path"], task["numbered"])
        except Exception as exc:  # one bad file must never fail the whole run
            logger.warning(
                "file_review_failed",
                file=task["path"],
                agent=agent,
                error=type(exc).__name__,
                # Provider error text (e.g. "credit balance is too low"); capped, one line, never
                # sent to GitHub: the PR only sees "review failed".
                detail=" ".join(str(exc).split())[:300],
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


def _validate(
    limits: ReviewLimits, known: tuple[KnownFinding, ...] = (), public: bool = False
) -> Any:
    def validate(state: ReviewState) -> dict[str, Any]:
        kept, dropped = partition_findings(state["findings"], state["to_review"])
        # Before the comment cap and before the gate: a repeat must not use a slot or ask a
        # maintainer to /confirm the same finding again.
        kept, repeats = drop_repeats(kept, known)
        dropped = dropped + repeats
        # Public repo: high/critical security findings leave the PR pipeline here, so they cannot
        # use a comment slot, trigger a /confirm hold or reach any PR text (ADR 0008).
        kept, private = divert(kept, public)
        kept, merged = aggregate(kept, limits.max_comments)
        if any(reason == OVER_CAP for _, reason in merged):
            logger.warning(
                "comment_cap_hit",
                kept=len(kept),
                dropped=sum(reason == OVER_CAP for _, reason in merged),
            )
        return {"validated": kept, "private": private, "dropped": dropped + merged}

    return validate


def _gate(hold_severities: frozenset[Severity], enabled: bool) -> Any:
    def gate(state: ReviewState) -> dict[str, Any]:
        if not enabled:
            return {}
        held = sum(f.severity in hold_severities for f in state["validated"])
        if held == 0:
            return {}
        # Nothing with side effects above this line: on resume this node runs again from the top.
        approved = interrupt({"held": held})
        if not approved:
            return {"validated": []}  # not approved: post nothing (fail closed)
        return {}

    return gate


def build_graph(
    reviewers: Mapping[str, Reviewer],
    limits: ReviewLimits,
    triager: Triager | None = None,
    *,
    hold_severities: frozenset[Severity] = frozenset(),
    checkpointer: Any = None,
    known: tuple[KnownFinding, ...] = (),
    public: bool = False,
) -> Any:
    graph = StateGraph(ReviewState)
    graph.add_node("prepare", _prepare(limits))
    graph.add_node("triage", _triage(triager))
    graph.add_node("review_file", _review_file(reviewers, limits))
    graph.add_node("validate", _validate(limits, known, public))
    graph.add_edge(START, "prepare")
    graph.add_edge("prepare", "triage")
    agents = [
        name for name in reviewers if ":" not in name
    ]  # "security:low" is a tier, not an agent
    graph.add_conditional_edges("triage", _fan_out(agents), ["review_file", "validate"])
    graph.add_node("gate", _gate(hold_severities, enabled=checkpointer is not None))
    graph.add_edge("review_file", "validate")
    graph.add_edge("validate", "gate")
    graph.add_edge("gate", END)
    return graph.compile(checkpointer=checkpointer)


def _held_count(validated: list[Finding], hold_severities: frozenset[Severity]) -> int:
    return sum(f.severity in hold_severities for f in validated)


def _to_outcome(
    values: Mapping[str, Any], pending: int, hold_severities: frozenset[Severity] = frozenset()
) -> ReviewOutcome:
    failed = {path for path, _ in values["errors"]}
    reviewed = {p: f for p, f in values["to_review"].items() if p not in failed}
    return ReviewOutcome(
        # Fail closed: a paused outcome carries only the findings that are NOT held, so nobody can
        # post a held one by mistake. (ADR 0007, amended in 10.8: the rest is not held.)
        findings=[f for f in values["validated"] if f.severity not in hold_severities]
        if pending
        else values["validated"],
        held=[f for f in values["validated"] if f.severity in hold_severities] if pending else [],
        reviewed=reviewed,
        # The checkpointer's serializer turns tuples into lists: restore them.
        skipped=list(
            dict.fromkeys(
                (path, reason)
                for path, reason in values["pre_skipped"]
                + values["limit_skipped"]
                + values["errors"]
            )
        ),
        failed=sorted(failed),
        dropped=[(item, reason) for item, reason in values["dropped"]],
        private=list(values.get("private", [])),
        raw_count=len(values["findings"]),
        risk=values["risk"],
        pending=pending,
    )


async def review_diff(
    files: Mapping[str, ParsedFile],
    reviewers: Reviewer | Mapping[str, Reviewer],
    limits: ReviewLimits,
    *,
    triager: Triager | None = None,
    run_name: str | None = None,
    metadata: Mapping[str, str] | None = None,
    callbacks: list[BaseCallbackHandler] | None = None,
    hold_severities: frozenset[Severity] = frozenset(),
    checkpointer: Any = None,
    thread_id: str | None = None,
    pre_skipped: list[tuple[str, str]] | None = None,
    known: tuple[KnownFinding, ...] = (),
    public: bool = False,
) -> ReviewOutcome:
    """Review parsed files. Evals call this directly; it knows nothing about GitHub.

    `reviewers` is one reviewer (treated as the security agent) or {agent name: reviewer}; every
    file goes to every agent, in parallel. run_name / metadata / callbacks only label and observe
    the run (tracing, token counting).
    """
    by_agent = {Agent.SECURITY.value: reviewers} if callable(reviewers) else dict(reviewers)
    app = build_graph(
        by_agent,
        limits,
        triager,
        hold_severities=hold_severities,
        checkpointer=checkpointer,
        known=known,
        public=public,
    )
    initial: ReviewState = {
        "files": dict(files),
        "to_review": {},
        "risk": {},
        "limit_skipped": [],
        "findings": [],
        "errors": [],
        "validated": [],
        "private": [],
        "dropped": [],
        "pre_skipped": list(pre_skipped or []),
    }
    config: dict[str, Any] = {"max_concurrency": MAX_CONCURRENT_FILES}
    if run_name:
        config["run_name"] = run_name
    if metadata:
        config["metadata"] = dict(metadata)
    if callbacks:
        config["callbacks"] = callbacks
    if checkpointer is not None:
        if thread_id is None:
            raise ValueError("thread_id is required with a checkpointer")
        config["configurable"] = {"thread_id": thread_id}
        existing = await app.aget_state(config)
        if existing.next:  # already paused: a job retry. Do not start again.
            return _to_outcome(
                existing.values,
                _held_count(existing.values["validated"], hold_severities),
                hold_severities,
            )
        if existing.values:
            # Finished earlier (stale). Without this the findings reducer appends duplicates.
            await checkpointer.adelete_thread(thread_id)

    final = await app.ainvoke(initial, config=config)
    if checkpointer is not None:
        snap = await app.aget_state(config)
        if snap.next:  # the gate paused
            return _to_outcome(
                snap.values, _held_count(snap.values["validated"], hold_severities), hold_severities
            )
    return _to_outcome(final, 0)


async def resume_review(
    thread_id: str,
    reviewers: Reviewer | Mapping[str, Reviewer],
    limits: ReviewLimits,
    hold_severities: frozenset[Severity],
    checkpointer: Any,
    approved: bool,
) -> ReviewOutcome:
    """Continue a review paused at the gate. The model is not called again.

    approved=False yields an outcome with NO findings; the caller must then post NOTHING (not even
    a "No findings" review, which would be false). The /confirm path only ever resumes with True.
    """
    by_agent = {Agent.SECURITY.value: reviewers} if callable(reviewers) else dict(reviewers)
    app = build_graph(
        by_agent, limits, None, hold_severities=hold_severities, checkpointer=checkpointer
    )
    config: dict[str, Any] = {"configurable": {"thread_id": thread_id}}
    snapshot = await app.aget_state(config)
    if not snapshot.next:
        if not snapshot.values:
            raise ValueError("no paused review for this thread")  # deleted or never held
        # Already resumed once (e.g. posting failed afterwards and the job retries): the saved
        # result is final, so return it instead of running the graph again.
        return _to_outcome(snapshot.values, 0)
    final = await app.ainvoke(Command(resume=approved), config=config)
    return _to_outcome(final, 0)
