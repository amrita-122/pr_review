"""Enqueue side of the job queue. Runs inside the webhook's database transaction."""

import uuid

import structlog
from sqlalchemy.orm import Session

from app.config import Settings
from app.db import runs
from app.db.models import Job
from app.github.webhooks import PullRequestEvent
from app.jobs.payload import REVIEW_PR, ReviewJobPayload

logger = structlog.get_logger()


def enqueue_review(session: Session, event: PullRequestEvent, settings: Settings) -> bool:
    """Queue a review for this PR head. Adds rows but does NOT commit.

    The caller commits the delivery record, the run and the job in ONE transaction, so a crash can
    never leave a recorded delivery without its job (that would be a lost review).
    Returns False when nothing needs to run (the same commit is already queued, running or done).
    """
    repo = f"{event.owner}/{event.repo}"
    # A newer push replaces older in-flight runs of this PR, whatever happens to this commit.
    superseded = runs.supersede_older(session, repo, event.number, event.head_sha)
    if superseded:
        logger.info("runs_superseded", count=superseded, repo=repo, pr_number=event.number)

    existing = runs.get_by_key(session, repo, event.number, event.head_sha)
    if existing is None:
        run_id = uuid.uuid4().hex
        runs.create_queued(
            session,
            run_id=run_id,
            repo=repo,
            pr_number=event.number,
            head_sha=event.head_sha,
            prompt_version=settings.prompt_version,
            model=settings.model_review,
        )
    elif existing.status in (runs.QUEUED, runs.RUNNING, runs.OK):
        return False
    else:  # failed or superseded earlier, and the same commit is back (reopened): run it again
        run_id = existing.id
        existing.status = runs.QUEUED
        existing.error = None
        existing.failure_reason = None
        existing.finished_at = None
        existing.failure_comment_posted = False

    payload = ReviewJobPayload.from_event(event, run_id)
    session.add(
        Job(kind=REVIEW_PR, payload=payload.model_dump(), max_attempts=settings.job_max_attempts)
    )
    return True
