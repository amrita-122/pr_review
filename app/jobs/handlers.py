import logging

import httpx

from app.config import get_settings
from app.diff.filters import skip_reason
from app.diff.parse import DiffParseError, ParsedFile, parse_patch
from app.github.auth import get_installation_token
from app.github.client import ReviewComment, list_pr_files, post_review
from app.github.webhooks import PullRequestEvent
from app.review.graph import ReviewLimits, review_diff
from app.review.llm import Reviewer, build_chat_model, load_prompt, make_reviewer
from app.review.render import render_comment, render_summary

logger = logging.getLogger(__name__)


def build_reviewer() -> Reviewer:
    """The one place the real model is created. Tests replace this with a fake."""
    settings = get_settings()
    return make_reviewer(
        build_chat_model(settings),
        prompt=load_prompt(),
        timeout_seconds=settings.model_timeout_seconds,
    )


async def process_pull_request(event: PullRequestEvent) -> None:
    """Background job. Must never raise: nobody is waiting on it."""
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
                logger.info("skipped %r: %s", changed.filename, reason)

        settings = get_settings()
        reviewer = build_reviewer()
        limits = ReviewLimits(settings.max_files_per_pr, settings.max_chars_per_file)
        outcome = await review_diff(parsed_files, reviewer, limits)

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
            logger.warning("inline comments rejected (422); posting summary only")
            await post_review(*args, summary, [])
    except Exception:
        logger.exception("failed to process PR %s/%s#%s", event.owner, event.repo, event.number)
