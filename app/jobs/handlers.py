"""The review job: fetch the PR, review it, post ONE review.

`run_review_job` is called by the worker (app/jobs/worker.py) and RAISES a classified JobFailure on
any failure; the worker decides about retries. It is safe to run twice for the same run: it will
not post a second review (see _post_once), and it will not post for a superseded run.
"""

from collections.abc import Callable, Mapping
from contextlib import AbstractAsyncContextManager
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import structlog
from langchain_core.callbacks import UsageMetadataCallbackHandler
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.db import runs
from app.db.session import get_session
from app.diff.filters import skip_reason
from app.diff.parse import DiffParseError, ParsedFile, parse_patch
from app.github.auth import get_installation_token, invalidate_installation_token
from app.github.client import (
    FILES_PER_PAGE,
    ReviewComment,
    get_pr_head_sha,
    list_pr_files,
    post_pr_comment,
    post_review,
    review_exists,
)
from app.jobs.errors import classify
from app.jobs.payload import ReviewJobPayload
from app.review.aggregate import MERGED, OVER_CAP
from app.review.checkpoint import delete_thread, open_checkpointer
from app.review.cost import cost_from_metadata
from app.review.graph import ReviewLimits, review_diff
from app.review.llm import Reviewer, build_review_setup
from app.review.render import render_comment, render_summary
from app.review.triage import Triager

logger = structlog.get_logger()

FAILURE_COMMENT = "Review failed. Push a new commit to try again."
EXPIRED_COMMENT = (
    "Held findings expired without a maintainer's confirmation and were not posted. "
    "Push a new commit to run the review again."
)


def build_reviewers() -> Mapping[str, Reviewer]:
    """The one place the job creates the real models. Tests replace this with a fake."""
    return build_review_setup(get_settings()).reviewers


def build_triager() -> Triager | None:
    """The cheap-model triager, or None when triage is off (the default)."""
    return build_review_setup(get_settings()).triager


def review_marker(run_id: str) -> str:
    """Hidden marker in our own review body: how a retry recognises a review it already posted."""
    return f"<!-- review-run:{run_id} -->"


def _critical[T](fn: Callable[[Session], T]) -> T:
    """A state change the job depends on. If the database fails, the job fails (and is retried)."""
    with get_session() as session:
        return fn(session)


def _record(action: str, fn: Callable[[Session], None]) -> None:
    """Bookkeeping (findings, usage). Must never break or hide a review."""
    try:
        with get_session() as session:
            fn(session)
    except Exception as exc:
        logger.warning("run_record_failed", action=action, error=type(exc).__name__)


def _bind_context(payload: ReviewJobPayload) -> None:
    # Adds to the worker's context (job_id) instead of replacing it: the job runs in its own task,
    # which got a copy, so nothing leaks into the next job.
    structlog.contextvars.bind_contextvars(
        run_id=payload.run_id,
        repo=payload.repo_full_name,
        pr_number=payload.number,
        head_sha=payload.head_sha,
    )


async def run_review_job(payload: ReviewJobPayload) -> str:
    """Review one PR head. Returns "posted", "already_posted" or "superseded"; raises JobFailure."""
    _bind_context(payload)
    settings = get_settings()
    usage_handler = UsageMetadataCallbackHandler()
    try:
        return await _run(payload, settings, usage_handler)
    except Exception as exc:
        failure = classify(exc)
        if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code == 401:
            invalidate_installation_token(payload.installation_id)
        # No traceback and no exception text: both can carry URLs or secrets.
        logger.warning(
            "job_attempt_failed",
            reason=failure.reason,
            transient=failure.transient,
            error=failure.message,
        )
        raise failure from None
    finally:
        usage, cost = cost_from_metadata(usage_handler.usage_metadata, settings.price_table)
        _record(
            "usage",
            lambda s: runs.add_usage(
                s,
                payload.run_id,
                tokens_in=usage.tokens_in,
                tokens_out=usage.tokens_out,
                cost_usd=cost,
            ),
        )
        logger.info(
            "attempt_finished",
            tokens_in=usage.tokens_in,
            tokens_out=usage.tokens_out,
            cost_usd=str(cost),
        )


async def _run(
    payload: ReviewJobPayload, settings: Settings, usage_handler: UsageMetadataCallbackHandler
) -> str:
    run_id = payload.run_id
    if not _critical(
        lambda s: runs.mark_running(
            s, run_id, prompt_version=settings.prompt_version, model=settings.model_review
        )
    ):
        logger.info("run_skipped", reason="superseded or missing")
        return "superseded"

    token = await get_installation_token(payload.installation_id)
    files = await list_pr_files(
        token, payload.owner, payload.repo, payload.number, settings.max_listed_pages
    )

    parsed_files: dict[str, ParsedFile] = {}
    skipped: list[tuple[str, str]] = []
    if len(files) >= settings.max_listed_pages * FILES_PER_PAGE:
        skipped.append(("(more files)", "PR has more files than the listing limit"))
    for changed in files:
        reason = skip_reason(changed.filename, changed.status) or changed.skip_reason
        if reason is None and changed.patch is not None:
            try:
                parsed_files[changed.filename] = parse_patch(changed.filename, changed.patch)
            except DiffParseError:
                reason = "could not parse patch"
        if reason is not None:
            skipped.append((changed.filename, reason))
            logger.info("file_skipped", file=changed.filename, reason=reason)

    if _critical(lambda s: runs.is_superseded(s, run_id)):  # don't pay for a stale review
        return "superseded"

    outcome = await review_diff(
        parsed_files,
        build_reviewers(),
        ReviewLimits.from_settings(settings),
        triager=build_triager(),
        run_name=f"review {payload.repo_full_name}#{payload.number}",
        metadata={
            "repo": payload.repo_full_name,
            "pr_number": str(payload.number),
            "head_sha": payload.head_sha,
            "prompt_version": settings.prompt_version,
            "model": settings.model_review,
            "run_id": run_id,
        },
        callbacks=[usage_handler],
    )
    _record("findings", lambda s: runs.save_findings(s, run_id, outcome.findings, outcome.dropped))

    overflow = sum(reason == OVER_CAP for _, reason in outcome.dropped)
    merged = sum(reason == MERGED for _, reason in outcome.dropped)
    summary = render_summary(
        len(outcome.reviewed),
        outcome.findings,
        skipped + outcome.skipped,
        overflow=overflow,
        merged=merged,
    )
    comments = [
        ReviewComment(path=f.file_path, line=f.line, body=render_comment(f))
        for f in outcome.findings
    ]
    result = await _post_once(payload, token, summary, comments)
    if result != "superseded":
        _critical(lambda s: runs.finish_run(s, run_id, status=runs.OK))
    return result


async def _post_once(
    payload: ReviewJobPayload, token: str, summary: str, comments: list[ReviewComment]
) -> str:
    """Post the review unless it is stale or was already posted. Never posts twice."""
    run_id = payload.run_id
    # Authoritative staleness check: ask GitHub for the PR's CURRENT head right before posting.
    # (The database flag can lag behind by a few seconds; this closes that race.)
    current = await get_pr_head_sha(token, payload.owner, payload.repo, payload.number)
    if current != payload.head_sha or _critical(lambda s: runs.is_superseded(s, run_id)):
        _critical(lambda s: runs.mark_superseded(s, run_id))
        logger.info("run_superseded_before_post")
        return "superseded"

    run = _critical(lambda s: runs.get_run(s, run_id))
    marker = review_marker(run_id)
    if run is not None and run.posted_at is not None:
        return "already_posted"
    if await review_exists(
        token, payload.owner, payload.repo, payload.number, payload.head_sha, marker
    ):
        # A previous attempt posted, then died before recording it.
        _critical(lambda s: runs.mark_posted(s, run_id))
        logger.info("review_already_on_github")
        return "already_posted"

    body = f"{summary}\n\n{marker}"
    args = (token, payload.owner, payload.repo, payload.number, payload.head_sha)
    try:
        await post_review(*args, body, comments)
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code != 422 or not comments:
            raise
        # GitHub rejects the WHOLE review if one comment can't be placed. Never lose the review.
        logger.warning("inline_comments_rejected_posting_summary_only")
        await post_review(*args, body, [])
    _critical(lambda s: runs.mark_posted(s, run_id))
    return "posted"


async def post_failure_notice(payload: ReviewJobPayload) -> None:
    """One short static comment after the final failure. Best effort: never raises.

    Static text only: no exception message, no stack trace, nothing from the PR.
    """
    try:
        if not _critical(lambda s: runs.claim_failure_comment(s, payload.run_id)):
            return  # already posted once, or the run was superseded
        token = await get_installation_token(payload.installation_id)
        await post_pr_comment(token, payload.owner, payload.repo, payload.number, FAILURE_COMMENT)
    except Exception as exc:
        logger.warning("failure_notice_not_posted", error=type(exc).__name__)


async def expire_pending_reviews(
    settings: Settings,
    now: datetime | None = None,
    open_saver: Callable[[Settings], AbstractAsyncContextManager[Any]] = open_checkpointer,
) -> int:
    """Fail closed: held reviews nobody confirmed in time are dropped, never posted.

    Per run: expire (atomic, loses to a confirm that got there first) -> delete the checkpoint
    (it holds source code) -> ONE static comment. Best effort per run: one failure must not stop
    the others. Returns how many runs expired.
    """
    if not settings.confirmation_enabled:
        return 0
    cutoff = (now or datetime.now(UTC)) - timedelta(hours=settings.confirm_ttl_hours)
    due = _critical(
        lambda s: [
            (r.id, r.repo, r.pr_number, r.installation_id) for r in runs.list_expired(s, cutoff)
        ]
    )
    expired = 0
    for run_id, repo_full_name, number, installation_id in due:
        try:
            if not _critical(lambda s, rid=run_id: runs.expire_run(s, rid)):  # type: ignore[misc]
                continue  # confirmed or superseded in the meantime
            expired += 1
            structlog.contextvars.bind_contextvars(run_id=run_id)
            logger.info("held_review_expired")
            async with open_saver(settings) as saver:
                await delete_thread(saver, run_id)
            if installation_id is not None and _critical(
                lambda s, rid=run_id: runs.claim_expired_comment(s, rid)  # type: ignore[misc]
            ):
                owner, _, repo = repo_full_name.partition("/")
                token = await get_installation_token(installation_id)
                await post_pr_comment(token, owner, repo, number, EXPIRED_COMMENT)
        except Exception as exc:
            logger.warning("expire_pending_failed", run_id=run_id, error=type(exc).__name__)
        finally:
            structlog.contextvars.unbind_contextvars("run_id")
    return expired
