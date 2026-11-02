import uuid
from collections.abc import Callable
from decimal import Decimal

import httpx
import structlog
from langchain_core.callbacks import UsageMetadataCallbackHandler
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.db import runs
from app.db.session import get_session
from app.diff.filters import skip_reason
from app.diff.parse import DiffParseError, ParsedFile, parse_patch
from app.github.auth import get_installation_token
from app.github.client import ReviewComment, list_pr_files, post_review
from app.github.webhooks import PullRequestEvent
from app.review.cost import Prices, Usage, estimate_cost, usage_from_metadata
from app.review.graph import ReviewLimits, review_diff
from app.review.llm import (
    Reviewer,
    build_default_reviewer,
)
from app.review.render import render_comment, render_summary

logger = structlog.get_logger()


def build_reviewer() -> Reviewer:
    """The one place the job creates the real model. Tests replace this with a fake."""
    return build_default_reviewer(get_settings())


def _record(action: str, fn: Callable[[Session], None]) -> None:
    """Run a DB write in its own session. Bookkeeping must never break or hide a review."""
    try:
        with get_session() as session:
            fn(session)
    except Exception as exc:
        logger.warning("run_record_failed", action=action, error=type(exc).__name__)


async def process_pull_request(event: PullRequestEvent) -> None:
    """Background job. Must never raise: nobody is waiting on it."""
    run_id = uuid.uuid4().hex
    structlog.contextvars.clear_contextvars()
    structlog.contextvars.bind_contextvars(
        run_id=run_id,
        repo=f"{event.owner}/{event.repo}",
        pr_number=event.number,
        head_sha=event.head_sha,
    )
    settings = get_settings()
    usage_handler = UsageMetadataCallbackHandler()
    _record(
        "start",
        lambda s: runs.start_run(
            s,
            run_id=run_id,
            repo=f"{event.owner}/{event.repo}",
            pr_number=event.number,
            head_sha=event.head_sha,
            prompt_version=settings.prompt_version,
            model=settings.model_review,
        ),
    )
    status, error = "failed", None
    try:
        token = await get_installation_token(event.installation_id)
        files = await list_pr_files(token, event.owner, event.repo, event.number)

        parsed_files: dict[str, ParsedFile] = {}
        skipped: list[tuple[str, str]] = []
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

        reviewer = build_reviewer()
        limits = ReviewLimits(settings.max_files_per_pr, settings.max_chars_per_file)
        outcome = await review_diff(
            parsed_files,
            reviewer,
            limits,
            run_name=f"review {event.owner}/{event.repo}#{event.number}",
            metadata={
                "repo": f"{event.owner}/{event.repo}",
                "pr_number": str(event.number),
                "head_sha": event.head_sha,
                "prompt_version": settings.prompt_version,
                "model": settings.model_review,
                "run_id": run_id,
            },
            callbacks=[usage_handler],
        )
        _record(
            "findings", lambda s: runs.save_findings(s, run_id, outcome.findings, outcome.dropped)
        )

        valid = outcome.findings
        skipped += outcome.skipped
        summary = render_summary(len(outcome.reviewed), valid, skipped)
        comments = [
            ReviewComment(path=f.file_path, line=f.line, body=render_comment(f)) for f in valid
        ]

        args = (token, event.owner, event.repo, event.number, event.head_sha)
        try:
            await post_review(*args, summary, comments)
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code != 422 or not comments:
                raise
            # GitHub rejects the WHOLE review if one comment can't be placed. Never lose the review.
            logger.warning("inline_comments_rejected_posting_summary_only")
            await post_review(*args, summary, [])
        status = "ok"
    except Exception as exc:
        error = f"{type(exc).__name__}: {str(exc)[:200]}"
        logger.exception("pr_processing_failed")
    finally:
        usage = usage_from_metadata(usage_handler.usage_metadata)
        cost = _cost(usage, settings)
        _record(
            "finish",
            lambda s: runs.finish_run(
                s,
                run_id,
                status=status,
                tokens_in=usage.tokens_in,
                tokens_out=usage.tokens_out,
                cost_usd=cost,
                error=error,
            ),
        )
        logger.info(
            "run_finished",
            status=status,
            tokens_in=usage.tokens_in,
            tokens_out=usage.tokens_out,
            cost_usd=str(cost),
        )


def _cost(usage: Usage, settings: Settings) -> Decimal:
    prices = Prices(settings.price_input_per_mtok, settings.price_output_per_mtok)
    return estimate_cost(usage, prices)
