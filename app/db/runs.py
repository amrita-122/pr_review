"""Persistence for review runs and findings.

State transitions here are what make retries safe: a superseded run is never revived by a retry,
posting is recorded the moment it succeeds, and the failure comment can be claimed only once.
Callers decide which writes are critical (let them raise) and which are bookkeeping (swallow).
"""

from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import delete, or_, select, update
from sqlalchemy.orm import Session

from app.db.models import FindingRow, ReviewRun
from app.review.schemas import Finding

MAX_ERROR_CHARS = 500

QUEUED = "queued"
RUNNING = "running"
OK = "ok"
FAILED = "failed"
SUPERSEDED = "superseded"
AWAITING_CONFIRMATION = "awaiting_confirmation"  # held for a maintainer's /confirm (Phase 8)
CONFIRMING = "confirming"  # a /confirm was accepted; the held review is being posted
EXPIRED = "expired"  # nobody confirmed in time; never posted
# A newer push replaces all of these. (A held or confirming run for an old commit must not post.)
IN_FLIGHT = (QUEUED, RUNNING, AWAITING_CONFIRMATION, CONFIRMING)


def get_run(session: Session, run_id: str) -> ReviewRun | None:
    return session.get(ReviewRun, run_id)


def get_by_key(session: Session, repo: str, pr_number: int, head_sha: str) -> ReviewRun | None:
    return session.scalars(
        select(ReviewRun).where(
            ReviewRun.repo == repo,
            ReviewRun.pr_number == pr_number,
            ReviewRun.head_sha == head_sha,
        )
    ).first()


def create_queued(
    session: Session,
    *,
    run_id: str,
    repo: str,
    pr_number: int,
    head_sha: str,
    prompt_version: str,
    model: str,
    head_updated_at: datetime | None = None,
) -> None:
    """Add (not commit) a queued run: the caller commits it together with the job."""
    session.add(
        ReviewRun(
            id=run_id,
            repo=repo,
            pr_number=pr_number,
            head_sha=head_sha,
            head_updated_at=head_updated_at,
            status=QUEUED,
            prompt_version=prompt_version,
            model=model,
        )
    )


def has_newer_run(
    session: Session, repo: str, pr_number: int, head_sha: str, updated_at: datetime | None
) -> bool:
    """True if this PR already has a run for ANOTHER commit that GitHub stamped strictly later.

    Then the incoming event is a late delivery of an older push and must not replace anything.
    Equal timestamps (two pushes in the same second) count as "not newer": arrival order decides.
    """
    if updated_at is None:
        return False
    return (
        session.scalars(
            select(ReviewRun.id)
            .where(
                ReviewRun.repo == repo,
                ReviewRun.pr_number == pr_number,
                ReviewRun.head_sha != head_sha,
                ReviewRun.head_updated_at > updated_at,
            )
            .limit(1)
        ).first()
        is not None
    )


def supersede_older(
    session: Session, repo: str, pr_number: int, keep_sha: str, updated_at: datetime | None = None
) -> int:
    """A newer push replaces in-flight runs of OLDER commits of the same PR (no commit).

    A run is older when its timestamp is not later than the incoming one; runs with no
    timestamp (created before the column existed) are treated as older.
    """
    conditions = [
        ReviewRun.repo == repo,
        ReviewRun.pr_number == pr_number,
        ReviewRun.head_sha != keep_sha,
        ReviewRun.status.in_(IN_FLIGHT),
    ]
    if updated_at is not None:
        conditions.append(
            or_(ReviewRun.head_updated_at.is_(None), ReviewRun.head_updated_at <= updated_at)
        )
    result = session.execute(
        update(ReviewRun)
        .where(*conditions)
        .values(status=SUPERSEDED, finished_at=datetime.now(UTC))
    )
    return int(result.rowcount)  # type: ignore[attr-defined]


def mark_running(session: Session, run_id: str, *, prompt_version: str, model: str) -> bool:
    """queued/failed/running -> running. False if the run is gone or was superseded."""
    run = session.get(ReviewRun, run_id)
    if run is None or run.status == SUPERSEDED:
        return False
    run.status = RUNNING
    run.prompt_version = prompt_version
    run.model = model
    session.commit()
    return True


def mark_superseded(session: Session, run_id: str) -> None:
    run = session.get(ReviewRun, run_id)
    if run is not None and run.status != SUPERSEDED:
        run.status = SUPERSEDED
        run.finished_at = datetime.now(UTC)
        session.commit()


def is_superseded(session: Session, run_id: str) -> bool:
    run = session.get(ReviewRun, run_id)
    return run is not None and run.status == SUPERSEDED


def set_retrying(session: Session, run_id: str, *, error: str, reason: str) -> None:
    """An attempt failed but the job will be retried: back to queued, remember why."""
    run = session.get(ReviewRun, run_id)
    if run is None or run.status == SUPERSEDED:
        return
    run.status = QUEUED
    run.error = error[:MAX_ERROR_CHARS]
    run.failure_reason = reason
    session.commit()


def mark_posted(session: Session, run_id: str) -> None:
    run = session.get(ReviewRun, run_id)
    if run is not None:
        run.posted_at = datetime.now(UTC)
        session.commit()


def add_usage(
    session: Session, run_id: str, *, tokens_in: int, tokens_out: int, cost_usd: Decimal
) -> None:
    """Accumulate across attempts: a retried run really did spend both attempts' tokens."""
    run = session.get(ReviewRun, run_id)
    if run is None:
        return
    run.tokens_in += tokens_in
    run.tokens_out += tokens_out
    run.cost_usd += cost_usd
    session.commit()


def finish_run(
    session: Session,
    run_id: str,
    *,
    status: str,
    error: str | None = None,
    failure_reason: str | None = None,
) -> None:
    run = session.get(ReviewRun, run_id)
    if run is None or run.status == SUPERSEDED:
        return  # a superseded run stays superseded
    run.status = status
    run.finished_at = datetime.now(UTC)
    # Exception type + short message only: never a stack trace (may hold URLs or secrets).
    run.error = error[:MAX_ERROR_CHARS] if error else None
    run.failure_reason = failure_reason
    session.commit()


def claim_failure_comment(session: Session, run_id: str) -> bool:
    """True exactly once per run, and never for a superseded run: the right to post the notice."""
    result = session.execute(
        update(ReviewRun)
        .where(
            ReviewRun.id == run_id,
            ReviewRun.failure_comment_posted.is_(False),
            ReviewRun.status != SUPERSEDED,
        )
        .values(failure_comment_posted=True)
    )
    session.commit()
    return bool(result.rowcount)  # type: ignore[attr-defined]


def save_findings(
    session: Session,
    run_id: str,
    posted: list[Finding],
    dropped: list[tuple[Finding, str]],
) -> None:
    session.execute(delete(FindingRow).where(FindingRow.run_id == run_id))  # a retry replaces
    rows = [(f, True, None) for f in posted] + [(f, False, reason) for f, reason in dropped]
    session.add_all(
        FindingRow(
            run_id=run_id,
            file=f.file_path,
            line=f.line,
            severity=f.severity.value,
            category=f.category.value,
            title=f.title,
            posted=is_posted,
            drop_reason=reason,
        )
        for f, is_posted, reason in rows
    )
    session.commit()


# --- Phase 8: human confirmation ------------------------------------------------------------


def mark_awaiting(session: Session, run_id: str, *, installation_id: int) -> bool:
    """running -> awaiting_confirmation. False if the run is gone or no longer running
    (a superseded run must not become pending)."""
    result = session.execute(
        update(ReviewRun)
        .where(ReviewRun.id == run_id, ReviewRun.status.in_((RUNNING, AWAITING_CONFIRMATION)))
        .values(
            status=AWAITING_CONFIRMATION,
            installation_id=installation_id,
            awaiting_since=datetime.now(UTC),
        )
    )
    session.commit()
    return bool(result.rowcount)  # type: ignore[attr-defined]


def get_awaiting_for_pr(session: Session, repo: str, pr_number: int) -> ReviewRun | None:
    """The run of this PR waiting for /confirm, if any (a push supersedes older ones)."""
    return session.scalars(
        select(ReviewRun)
        .where(
            ReviewRun.repo == repo,
            ReviewRun.pr_number == pr_number,
            ReviewRun.status == AWAITING_CONFIRMATION,
        )
        .order_by(ReviewRun.started_at.desc())
    ).first()


def claim_confirmation(session: Session, run_id: str, *, login: str, now: datetime) -> bool:
    """awaiting_confirmation -> confirming, atomically. True for exactly ONE caller per hold."""
    result = session.execute(
        update(ReviewRun)
        .where(ReviewRun.id == run_id, ReviewRun.status == AWAITING_CONFIRMATION)
        .values(status=CONFIRMING, confirmed_by=login, confirmed_at=now)
    )
    session.commit()
    return bool(result.rowcount)  # type: ignore[attr-defined]


def revert_to_awaiting(session: Session, run_id: str) -> None:
    """A confirm attempt failed: the review is still held, so a maintainer can try again."""
    session.execute(
        update(ReviewRun)
        .where(ReviewRun.id == run_id, ReviewRun.status == CONFIRMING)
        .values(status=AWAITING_CONFIRMATION, confirmed_by=None, confirmed_at=None)
    )
    session.commit()


def claim_pending_comment(session: Session, run_id: str) -> bool:
    """True exactly once per run: the right to post the 'N findings pending' comment."""
    result = session.execute(
        update(ReviewRun)
        .where(
            ReviewRun.id == run_id,
            ReviewRun.pending_comment_posted.is_(False),
            ReviewRun.status == AWAITING_CONFIRMATION,
        )
        .values(pending_comment_posted=True)
    )
    session.commit()
    return bool(result.rowcount)  # type: ignore[attr-defined]


def expire_run(session: Session, run_id: str) -> bool:
    """awaiting_confirmation -> expired, atomically (a confirm that won the race keeps its run)."""
    result = session.execute(
        update(ReviewRun)
        .where(ReviewRun.id == run_id, ReviewRun.status == AWAITING_CONFIRMATION)
        .values(status=EXPIRED, finished_at=datetime.now(UTC))
    )
    session.commit()
    return bool(result.rowcount)  # type: ignore[attr-defined]


def claim_expired_comment(session: Session, run_id: str) -> bool:
    """True exactly once per run: the right to post the 'expired' comment."""
    result = session.execute(
        update(ReviewRun)
        .where(
            ReviewRun.id == run_id,
            ReviewRun.expired_comment_posted.is_(False),
            ReviewRun.status == EXPIRED,
        )
        .values(expired_comment_posted=True)
    )
    session.commit()
    return bool(result.rowcount)  # type: ignore[attr-defined]


def list_expired(session: Session, cutoff: datetime) -> list[ReviewRun]:
    """Held runs older than the cutoff. The caller expires each with expire_run()."""
    return list(
        session.scalars(
            select(ReviewRun).where(
                ReviewRun.status == AWAITING_CONFIRMATION, ReviewRun.awaiting_since < cutoff
            )
        ).all()
    )


def list_superseded_holds(session: Session, repo: str, pr_number: int) -> list[str]:
    """Ids of held runs of this PR that a newer push replaced: their checkpoints can go."""
    return list(
        session.scalars(
            select(ReviewRun.id).where(
                ReviewRun.repo == repo,
                ReviewRun.pr_number == pr_number,
                ReviewRun.status == SUPERSEDED,
                ReviewRun.awaiting_since.is_not(None),
            )
        ).all()
    )
