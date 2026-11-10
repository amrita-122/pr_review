"""The review job: fetch the PR, review it, post ONE review.

`run_review_job` is called by the worker (app/jobs/worker.py) and RAISES a classified JobFailure on
any failure; the worker decides about retries. It is safe to run twice for the same run: it will
not post a second review (see _post_once), and it will not post for a superseded run.
"""

from collections.abc import AsyncIterator, Callable, Mapping
from contextlib import AbstractAsyncContextManager, asynccontextmanager
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
from app.github.advisories import create_draft_advisory
from app.github.auth import get_installation_token, invalidate_installation_token
from app.github.client import (
    ReviewComment,
    get_collaborator_permission,
    get_pr_head_sha,
    list_pr_files,
    post_pr_comment,
    post_review,
    reply_to_review_comment,
    resolve_review_thread,
    review_exists,
)
from app.jobs.errors import MODEL, JobFailure, classify
from app.jobs.payload import ConfirmJobPayload, ReviewJobPayload
from app.review.aggregate import MERGED, OVER_CAP
from app.review.checkpoint import delete_thread, open_checkpointer
from app.review.cost import cost_from_metadata
from app.review.dedupe import ALREADY_POSTED, KnownFinding, is_repeat_of
from app.review.graph import ReviewLimits, ReviewOutcome, resume_review, review_diff
from app.review.llm import Reviewer, build_review_setup
from app.review.render import (
    render_comment,
    render_confirmed_summary,
    render_fallback_summary,
    render_summary,
)
from app.review.schemas import Severity
from app.review.triage import Triager
from app.review.validate import Dropped
from app.review.visibility import PRIVATE_DELIVERED, PRIVATE_FAILED

logger = structlog.get_logger()

FAILURE_COMMENT = "Review failed. Push a new commit to try again."
EXPIRED_COMMENT = (
    "Held findings expired without a maintainer's confirmation and were not posted. "
    "Push a new commit to run the review again."
)


DISMISSED_REPLY = "Marked as a false positive by a maintainer. This thread is resolved."
DISMISSED_HOLD_COMMENT = "A maintainer dismissed the held findings. They were not posted."
NOT_ALLOWED_COMMENT = "Only a collaborator with write access can confirm a held review."
AUTHOR_COMMENT = "The author of a pull request cannot confirm their own review."
MAX_REFUSAL_REPLIES_PER_RUN = 3  # many different people refused: stop answering, never reply-spam


async def _refuse(payload: ConfirmJobPayload, token: str, text: str) -> None:
    """Remember the refusal (so the webhook ignores repeats) and answer ONCE per person, with a
    cap per run. Best effort: a failed reply changes nothing."""
    run_id, login = payload.run_id, payload.commenter_login
    if not _critical(lambda s: runs.record_denial(s, run_id, login)):
        return
    if _critical(lambda s: runs.denial_count(s, run_id)) > MAX_REFUSAL_REPLIES_PER_RUN:
        return
    try:
        await post_pr_comment(token, payload.owner, payload.repo, payload.number, text)
    except Exception as exc:
        logger.warning("refusal_reply_not_posted", error=type(exc).__name__)


def pending_comment(held: int, severities: frozenset[Severity], public: bool = False) -> str:
    """Static text. It carries a count and our own severity names, never anything from the PR.
    On a public repo it names neither (ADR 0008): "critical" is exactly what must not leak."""
    if public:
        return (
            "Part of this review is pending. A maintainer with write access can reply `/confirm`."
        )
    names = "/".join(sorted(s.value for s in severities))
    what = f"{held} {names} finding(s)" if held > 0 else f"{names.capitalize()} finding(s)"
    return (
        f"{what} pending. The review is held until a maintainer with write access "
        "replies `/confirm`."
    )


def build_reviewers() -> Mapping[str, Reviewer]:
    """The one place the job creates the real models. Tests replace this with a fake."""
    return build_review_setup(get_settings()).reviewers


def build_triager() -> Triager | None:
    """The cheap-model triager, or None when triage is off (the default)."""
    return build_review_setup(get_settings()).triager


def review_marker(run_id: str, *, held: bool = False) -> str:
    """Hidden marker in our own review body: how a retry recognises a review it already posted.
    The follow-up review of confirmed findings has its own marker (neither contains the other)."""
    return f"<!-- review-run:{run_id}:held -->" if held else f"<!-- review-run:{run_id} -->"


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


def _invalidate_on_401(exc: BaseException, installation_id: int) -> None:
    """A 401 means our cached installation token died early: drop it so the next call mints one."""
    if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code == 401:
        invalidate_installation_token(installation_id)


async def run_review_job(payload: ReviewJobPayload) -> str:
    """Review one PR head. Returns "posted", "pending" (held), "already_posted", "superseded" or
    "already_handled"; raises JobFailure."""
    _bind_context(payload)
    settings = get_settings()
    usage_handler = UsageMetadataCallbackHandler()
    try:
        return await _run(payload, settings, usage_handler)
    except Exception as exc:
        failure = classify(exc)
        _invalidate_on_401(exc, payload.installation_id)
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


@asynccontextmanager
async def _checkpointer_scope(settings: Settings) -> AsyncIterator[Any]:
    """The checkpointer while human confirmation is on, otherwise None (graph runs as before)."""
    if not settings.confirmation_enabled:
        yield None
        return
    async with open_checkpointer(settings) as saver:
        yield saver


async def _post_pending_comment(
    run_id: str,
    installation_id: int,
    repo_full_name: str,
    number: int,
    held: int,
    settings: Settings,
) -> bool:
    """Claim the right to announce the hold, then post it. If the POST fails the claim is given
    back, so the next sweep tries again: a held review nobody was told about is a lost review.
    True = posted (or someone else already did)."""
    if not _critical(lambda s: runs.claim_pending_comment(s, run_id)):
        return True
    try:
        owner, _, repo = repo_full_name.partition("/")
        token = await get_installation_token(installation_id)
        run = _critical(lambda s: runs.get_run(s, run_id))
        public = run is None or run.repo_private is not True  # unknown is public
        text = pending_comment(held, settings.held_severities, public)
        await post_pr_comment(token, owner, repo, number, text)
    except Exception as exc:
        logger.warning("pending_comment_not_posted", error=type(exc).__name__)
        _invalidate_on_401(exc, installation_id)
        _critical(lambda s: runs.release_pending_comment(s, run_id))
        return False
    return True


async def _announce_pending(payload: ReviewJobPayload, held: int, settings: Settings) -> str:
    """The review is held. Record it and say so. Never raises after the hold is recorded:
    a failed comment must not re-run the (paid) review; the sweep retries the comment."""
    run_id = payload.run_id
    if not _critical(
        lambda s: runs.mark_awaiting(s, run_id, installation_id=payload.installation_id, held=held)
    ):
        return "superseded"  # a newer push replaced this run while it was reviewing
    logger.info("review_held", held=held)
    run = _critical(lambda s: runs.get_run(s, run_id))
    count = run.held_count if run is not None else held
    await _post_pending_comment(
        run_id, payload.installation_id, payload.repo_full_name, payload.number, count, settings
    )
    return "pending"


async def announce_unannounced_holds(settings: Settings) -> int:
    """Housekeeping: re-send the 'pending' comment for held runs whose first attempt failed.
    Returns how many were posted."""
    if not settings.confirmation_enabled:
        return 0
    posted = 0
    due = _critical(lambda s: runs.list_unannounced(s))
    for run_id, repo_full_name, number, installation_id, held in due:
        if await _post_pending_comment(
            run_id, installation_id, repo_full_name, number, held, settings
        ):
            posted += 1
    return posted


async def _drop_superseded_holds(saver: Any, payload: ReviewJobPayload) -> None:
    """Checkpoints hold source code: remove those of held runs a newer push replaced."""
    stale = _critical(
        lambda s: runs.list_superseded_holds(s, payload.repo_full_name, payload.number)
    )
    for stale_id in stale:
        await delete_thread(saver, stale_id)


async def _run(
    payload: ReviewJobPayload, settings: Settings, usage_handler: UsageMetadataCallbackHandler
) -> str:
    run_id = payload.run_id
    # A retry of a job whose review already paused (or finished) must not start the review again.
    current = _critical(lambda s: runs.get_run(s, run_id))
    if current is not None and current.status == runs.AWAITING_CONFIRMATION:
        return await _announce_pending(payload, 0, settings)
    if current is not None and current.status in (runs.CONFIRMING, runs.EXPIRED, runs.OK):
        logger.info("run_skipped", reason=f"already {current.status}")
        return "already_handled"
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
    if files.truncated:
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

    # Findings already commented on this PR by earlier pushes are not posted again.
    known = tuple(
        KnownFinding(file=f, category=c, line=n)
        for f, c, n in _critical(
            lambda s: runs.list_known_findings(s, payload.repo_full_name, payload.number, run_id)
        )
    )
    async with _checkpointer_scope(settings) as saver:
        if saver is not None:
            await _drop_superseded_holds(saver, payload)
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
            hold_severities=settings.held_severities if saver is not None else frozenset(),
            checkpointer=saver,
            thread_id=run_id if saver is not None else None,
            pre_skipped=skipped,
            known=known,
            public=payload.is_public,
        )
        private_dropped = await _deliver_private(payload, token, outcome, settings)
        dropped = outcome.dropped + private_dropped
        if outcome.pending:
            # The held findings are neither saved nor posted until confirmed. The rest of the
            # review is NOT held (ADR 0007, amended): it goes out now, once.
            if outcome.findings:
                _record(
                    "findings",
                    lambda s: runs.save_findings(s, run_id, outcome.findings, dropped),
                )
                summary, comments = _render(outcome, held=outcome.pending, public=payload.is_public)
                if await _post_once(payload, token, summary, comments) == "superseded":
                    await delete_thread(saver, run_id)
                    return "superseded"
            result = await _announce_pending(payload, outcome.pending, settings)
            if result == "superseded":
                await delete_thread(saver, run_id)
            return result

    if outcome.failed and not outcome.reviewed:
        # Every file we tried to review failed: there is nothing to report. Retry later (and after
        # the last attempt the PR gets the short failure notice) instead of posting an empty review.
        raise JobFailure(MODEL, True, "every file failed review")

    _record("findings", lambda s: runs.save_findings(s, run_id, outcome.findings, dropped))
    summary, comments = _render(outcome, public=payload.is_public)
    result = await _post_once(payload, token, summary, comments)
    if result != "superseded":
        _critical(lambda s: runs.finish_run(s, run_id, status=runs.OK))
    if result == "posted":
        await _close_fixed_findings(payload, token, outcome)
    return result


FIXED_REPLY = "Fixed in `{sha}`. This thread is resolved; reopen it if the problem is still there."


async def _close_fixed_findings(
    payload: ReviewJobPayload, token: str, outcome: ReviewOutcome
) -> None:
    """Answer and resolve the threads of earlier findings this push no longer reproduces (10.16).

    Conservative on purpose: a model that stays silent is weak evidence, so nothing is closed
    unless this review was complete (no failed files) and covered the finding's file. Comments
    are never deleted or edited. Best effort: a failure here never fails the review job.
    """
    if outcome.failed:
        return
    run_id = payload.run_id
    try:
        prior = _critical(
            lambda s: runs.list_open_findings(s, payload.repo_full_name, payload.number, run_id)
        )
        present = [
            *outcome.findings,
            *outcome.held,
            *outcome.private,
            *(finding for finding, _ in outcome.dropped),
        ]
        sha = payload.head_sha
        for finding_id, file, category, line, comment_id in prior:
            if file not in outcome.reviewed:
                continue  # skipped, over a limit, or not in this push: no evidence either way
            if is_repeat_of(KnownFinding(file=file, category=category, line=line), present):
                continue
            if not _critical(lambda s, fid=finding_id: runs.claim_fixed(s, fid, sha[:12])):  # type: ignore[misc]
                continue
            if comment_id is None:
                continue  # we never learned GitHub's id: recorded as fixed, nothing to answer
            try:
                await reply_to_review_comment(
                    token,
                    payload.owner,
                    payload.repo,
                    payload.number,
                    comment_id,
                    FIXED_REPLY.format(sha=sha[:7]),
                )
                await resolve_review_thread(
                    token, payload.owner, payload.repo, payload.number, comment_id
                )
            except Exception as exc:
                logger.warning("fixed_reply_failed", error=type(exc).__name__)
                _critical(lambda s, fid=finding_id: runs.release_fixed(s, fid))  # type: ignore[misc]
    except Exception as exc:  # the review itself is already posted
        logger.warning("close_fixed_findings_failed", error=type(exc).__name__)


async def _post_once(
    payload: ReviewJobPayload,
    token: str,
    summary: str,
    comments: list[ReviewComment],
    *,
    held: bool = False,
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
    marker = review_marker(run_id, held=held)
    done_at = None if run is None else (run.held_posted_at if held else run.posted_at)
    if done_at is not None:
        return "already_posted"
    if await review_exists(
        token, payload.owner, payload.repo, payload.number, payload.head_sha, marker
    ):
        # A previous attempt posted, then died before recording it.
        _critical(lambda s: runs.mark_posted(s, run_id, held=held))
        logger.info("review_already_on_github")
        return "already_posted"

    body = f"{summary}\n\n{marker}"
    args = (token, payload.owner, payload.repo, payload.number, payload.head_sha)
    try:
        posted = await post_review(*args, body, comments)
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code != 422 or not comments:
            raise
        # GitHub rejects the WHOLE review if one comment can't be placed. Never lose the review.
        logger.warning("inline_comments_rejected_posting_summary_only")
        if await review_exists(*args, marker):  # re-check: never post a second review
            _critical(lambda s: runs.mark_posted(s, run_id, held=held))
            return "already_posted"
        fallback = render_fallback_summary(summary, [(c.path, c.line, c.body) for c in comments])
        posted = await post_review(*args, f"{fallback}\n\n{marker}", [])
    _critical(lambda s: runs.mark_posted(s, run_id, held=held))
    _record(
        "github_ids",
        lambda s: runs.save_github_ids(
            s,
            run_id,
            review_id=posted.id,
            comments=[
                (c.path, c.placed_line, c.id, c.node_id)
                for c in posted.comments
                if c.placed_line is not None
            ],
        ),
    )
    return "posted"


def _render(
    outcome: ReviewOutcome, held: int = 0, public: bool = False
) -> tuple[str, list[ReviewComment]]:
    """Review text from an outcome. `outcome.skipped` already includes the pre-graph skips."""
    overflow = sum(reason == OVER_CAP for _, reason in outcome.dropped)
    repeated = sum(reason == ALREADY_POSTED for _, reason in outcome.dropped)
    merged = sum(reason == MERGED for _, reason in outcome.dropped)
    summary = render_summary(
        len(outcome.reviewed),
        outcome.findings,
        outcome.skipped,
        overflow=overflow,
        merged=merged,
        failed=len(outcome.failed),
        held=held,
        repeated=repeated,
        public=public,
    )
    comments = [
        ReviewComment(path=f.file_path, line=f.line, body=render_comment(f))
        for f in outcome.findings
    ]
    return summary, comments


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
        _invalidate_on_401(exc, payload.installation_id)
        logger.warning("failure_notice_not_posted", error=type(exc).__name__)


async def run_confirm_job(payload: ConfirmJobPayload) -> str:
    try:
        return await _confirm(payload)
    except Exception as exc:
        _invalidate_on_401(exc, payload.installation_id)
        raise


async def _confirm(payload: ConfirmJobPayload) -> str:
    run_id = payload.run_id
    structlog.contextvars.bind_contextvars(run_id=run_id, pr_number=payload.number)
    settings = get_settings()

    # 0. Nobody confirms their own review, whatever their permission (the author is the party
    # the findings are about). Checked before any permission lookup.
    if payload.author_login is not None and payload.author_login.lower() == (
        payload.commenter_login.lower()
    ):
        logger.info("confirm_ignored", reason="author")
        await _refuse(
            payload, await get_installation_token(payload.installation_id), AUTHOR_COMMENT
        )
        return "ignored"

    # 1. Permission, asked of GitHub (the comment's own author_association is only a hint).
    token = await get_installation_token(payload.installation_id)
    permission = await get_collaborator_permission(
        token, payload.owner, payload.repo, payload.commenter_login
    )
    if not is_allowed(permission):
        logger.info("confirm_ignored", reason="not_allowed")
        await _refuse(payload, token, NOT_ALLOWED_COMMENT)
        return "ignored"  # not an error: a retry would not change the answer

    if payload.command == "dismiss":
        return await _dismiss(payload, token, settings)

    # 2. Claim: awaiting_confirmation -> confirming, atomically. Losing is not a failure.
    if not _critical(
        lambda s: runs.claim_confirmation(
            s, run_id, login=payload.commenter_login, now=datetime.now(UTC)
        )
    ):
        logger.info("confirm_ignored", reason="not_claimable")
        return "ignored"

    run = _critical(lambda s: runs.get_run(s, run_id))
    if run is None:
        return "ignored"
    review = ReviewJobPayload(
        run_id=run_id,
        installation_id=payload.installation_id,
        owner=payload.owner,
        repo=payload.repo,
        number=payload.number,
        head_sha=run.head_sha,
    )

    public = run.repo_private is not True  # unknown is public: the safe side

    # 3. A retry after a post that succeeded: do not resume a finished thread.
    # Two shapes of held run: the rest of the review went out BEFORE the hold was recorded
    # (10.8: posted_at < awaiting_since), or nothing did and the whole review waits for /confirm.
    follow_up = (
        run.posted_at is not None
        and run.awaiting_since is not None
        and run.posted_at < run.awaiting_since
    )
    if (run.held_posted_at if follow_up else run.posted_at) is not None:
        _critical(lambda s: runs.finish_run(s, run_id, status=runs.OK))
        async with open_checkpointer(settings) as saver:
            await delete_thread(saver, run_id)
        return "already_posted"

    # 4. Resume the held graph from Postgres. Nothing calls the model.
    async with open_checkpointer(settings) as saver:
        outcome = await resume_review(
            run_id,
            build_reviewers(),
            ReviewLimits.from_settings(settings),
            settings.held_severities,
            saver,
            approved=True,
        )

        # The held findings are only now final: record them (posted or not is set when posted).
        # After a first review (10.8) only the held severities are replaced: the others keep
        # their rows and GitHub ids.
        held_names = frozenset(s.value for s in settings.held_severities)
        _record(
            "findings",
            lambda s: runs.save_findings(
                s,
                run_id,
                [f for f in outcome.findings if not follow_up or f.severity.value in held_names],
                [] if follow_up else outcome.dropped,
                replace_severities=held_names if follow_up else None,
            ),
        )

        # 5. Render, 6. post once (re-checks head SHA and the marker). When the rest of the review
        # already went out (10.8), only the confirmed findings are posted, as a follow-up review.
        if follow_up:
            confirmed = [f for f in outcome.findings if f.severity in settings.held_severities]
            summary = render_confirmed_summary(confirmed, public)
            comments = [
                ReviewComment(path=f.file_path, line=f.line, body=render_comment(f))
                for f in confirmed
            ]
        else:
            summary, comments = _render(outcome, public=public)
        result = await _post_once(review, token, summary, comments, held=follow_up)

        # 7. Finish. _post_once already marked a superseded run.
        if result != "superseded":
            _critical(lambda s: runs.finish_run(s, run_id, status=runs.OK))

        # 8. The checkpoint holds source code: delete it in every end state.
        await delete_thread(saver, run_id)
    return result


def is_allowed(permission: str | None) -> bool:
    """Fail closed: only admin and write may confirm. Everything else, including None, may not."""
    return permission in {"admin", "write"}


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
            if installation_id is not None:
                _invalidate_on_401(exc, installation_id)
            logger.warning("expire_pending_failed", run_id=run_id, error=type(exc).__name__)
        finally:
            structlog.contextvars.unbind_contextvars("run_id")
    return expired


async def purge_closed_checkpoints(
    settings: Settings,
    open_saver: Callable[[Settings], AbstractAsyncContextManager[Any]] = open_checkpointer,
) -> int:
    """Delete the checkpoints of held runs whose PR closed (they hold source code).
    Retried on the next sweep when a deletion fails. Returns how many were purged."""
    if not settings.confirmation_enabled:
        return 0
    ids = _critical(lambda s: runs.list_checkpoints_to_purge(s))
    purged = 0
    for run_id in ids:
        async with open_saver(settings) as saver:
            if await delete_thread(saver, run_id):
                _critical(lambda s, rid=run_id: runs.mark_checkpoint_purged(s, rid))  # type: ignore[misc]
                purged += 1
    return purged


async def _dismiss(payload: ConfirmJobPayload, token: str, settings: Settings) -> str:
    """An allowed maintainer said /dismiss (permission already checked by the caller).

    - In a finding's thread: that finding becomes a false positive. It is never repeated and never
      called fixed; the thread gets one reply and is resolved. The comment is not deleted.
    - On the PR as a whole: the held review is dropped instead of waiting to expire.
    """
    run_id = payload.run_id
    if payload.target_comment_id is None:
        if not _critical(lambda s: runs.dismiss_held(s, run_id)):
            logger.info("dismiss_ignored", reason="not_held")
            return "ignored"
        logger.info("held_review_dismissed", by=payload.commenter_login)
        async with open_checkpointer(settings) as saver:  # source code: gone with the review
            if await delete_thread(saver, run_id):
                _critical(lambda s: runs.mark_checkpoint_purged(s, run_id))
        try:
            if _critical(lambda s: runs.claim_dismissed_comment(s, run_id)):
                await post_pr_comment(
                    token, payload.owner, payload.repo, payload.number, DISMISSED_HOLD_COMMENT
                )
        except Exception as exc:
            logger.warning("dismiss_comment_not_posted", error=type(exc).__name__)
        return "dismissed"

    found = _critical(
        lambda s: runs.find_posted_finding_by_comment(
            s, payload.repo_full_name, payload.number, payload.target_comment_id or 0
        )
    )
    if found is None:
        return "ignored"
    finding_id, _ = found
    if not _critical(lambda s: runs.dismiss_finding(s, finding_id, payload.commenter_login)):
        return "ignored"  # already dismissed (a redelivery or a second maintainer)
    logger.info("finding_dismissed", by=payload.commenter_login)
    try:
        await reply_to_review_comment(
            token, payload.owner, payload.repo, payload.number, payload.target_comment_id,
            DISMISSED_REPLY,
        )  # fmt: skip
        await resolve_review_thread(
            token, payload.owner, payload.repo, payload.number, payload.target_comment_id
        )
    except Exception as exc:
        # Keep the dismissal: the maintainer's decision stands even if our reply did not post.
        logger.warning("dismiss_reply_failed", error=type(exc).__name__)
    return "dismissed"


async def _deliver_private(
    payload: ReviewJobPayload, token: str, outcome: ReviewOutcome, settings: Settings
) -> list[Dropped]:
    """Public repo: send each diverted high/critical security finding to the private channel.

    Returns them as (finding, reason) for the database: PRIVATE_DELIVERED, or PRIVATE_FAILED. FAIL
    CLOSED: whatever goes wrong, nothing about the finding is written to the PR; it stays in the
    database for the maintainer. Idempotent across job retries (already-delivered ones are skipped).
    Only the count and the failure KIND are logged, never the finding.
    """
    if not outcome.private:
        return []
    run_id = payload.run_id
    done = _critical(lambda s: runs.delivered_private(s, run_id))
    results: list[Dropped] = []
    delivered = 0
    for finding in outcome.private:
        key = (finding.file_path, finding.category.value, finding.line)
        if key in done:
            results.append((finding, PRIVATE_DELIVERED))
            delivered += 1
            continue
        reason = PRIVATE_FAILED
        if settings.private_channel == "none":
            logger.warning("private_delivery_skipped", why="no private channel configured")
        elif delivered >= settings.max_private_per_run:
            logger.warning("private_delivery_skipped", why="over the per-review cap")
        else:
            try:
                await create_draft_advisory(
                    token, payload.owner, payload.repo, finding, payload.number, payload.head_sha
                )
                reason = PRIVATE_DELIVERED
                delivered += 1
            except Exception as exc:
                _invalidate_on_401(exc, payload.installation_id)
                status = (
                    exc.response.status_code if isinstance(exc, httpx.HTTPStatusError) else None
                )
                logger.warning("private_delivery_failed", error=type(exc).__name__, status=status)
        results.append((finding, reason))
    _record("private_count", lambda s: runs.set_private_count(s, run_id, len(results)))
    return results
