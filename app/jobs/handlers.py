import logging

import httpx

from app.diff.filters import skip_reason
from app.diff.parse import DiffParseError, ParsedFile, parse_patch
from app.github.auth import get_installation_token
from app.github.client import ReviewComment, list_pr_files, post_review
from app.github.webhooks import PullRequestEvent
from app.review.fake import review_file
from app.review.render import render_comment, render_summary
from app.review.schemas import Finding
from app.review.validate import validate_findings

logger = logging.getLogger(__name__)


async def process_pull_request(event: PullRequestEvent) -> None:
    """Background job. Must never raise: nobody is waiting on it."""
    try:
        token = await get_installation_token(event.installation_id)
        files = await list_pr_files(token, event.owner, event.repo, event.number)

        reviewed: dict[str, ParsedFile] = {}
        skipped: list[tuple[str, str]] = []
        findings: list[Finding] = []
        for changed in files:
            reason = skip_reason(changed.filename, changed.status) or changed.skip_reason
            if reason is None and changed.patch is not None:
                try:
                    parsed = parse_patch(changed.filename, changed.patch)
                except DiffParseError:
                    reason = "could not parse patch"
                else:
                    reviewed[changed.filename] = parsed
                    findings += review_file(changed.filename, parsed.numbered)
            if reason is not None:
                skipped.append((changed.filename, reason))
                logger.info("skipped %r: %s", changed.filename, reason)

        valid = validate_findings(findings, reviewed)
        summary = render_summary(len(reviewed), valid, skipped)
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
