"""Persistence for review runs and findings.

State transitions here are what make retries safe: a superseded run is never revived by a retry,
posting is recorded the moment it succeeds, and the failure comment can be claimed only once.
Callers decide which writes are critical (let them raise) and which are bookkeeping (swallow).
"""

from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import delete, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db.models import ConfirmDenial, FindingRow, ReviewRun
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
DISMISSED = "dismissed"  # a maintainer dismissed the held review; never posted
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
    Equal timestamps (two pushes in the same second) are not "newer": see supersede_older.
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

    A run is older when its timestamp is strictly earlier than the incoming one; runs with no
    timestamp (created before the column existed) are treated as older. Equal timestamps (two
    pushes in the same second) supersede nothing: order is unknowable, so both run and the
    stale one is dropped at post time, when GitHub is asked for the PR's current head.
    """
    conditions = [
        ReviewRun.repo == repo,
        ReviewRun.pr_number == pr_number,
        ReviewRun.head_sha != keep_sha,
        ReviewRun.status.in_(IN_FLIGHT),
    ]
    if updated_at is not None:
        conditions.append(
            or_(ReviewRun.head_updated_at.is_(None), ReviewRun.head_updated_at < updated_at)
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


def mark_posted(session: Session, run_id: str, *, held: bool = False) -> None:
    """Record a posted review. held=True is the follow-up review of the confirmed findings."""
    run = session.get(ReviewRun, run_id)
    if run is not None:
        if held:
            run.held_posted_at = datetime.now(UTC)
        else:
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
    *,
    replace_severities: frozenset[str] | None = None,
) -> None:
    """Record a run's findings; a retry replaces them. `replace_severities` limits what is
    replaced: the follow-up review after /confirm saves only the held severities and must keep
    the rows (and GitHub ids) of the findings that were posted in the first review."""
    stale = delete(FindingRow).where(FindingRow.run_id == run_id)
    if replace_severities is not None:
        stale = stale.where(FindingRow.severity.in_(replace_severities))
    session.execute(stale)
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


def mark_awaiting(
    session: Session, run_id: str, *, installation_id: int, held: int | None = None
) -> bool:
    """running -> awaiting_confirmation. False if the run is gone or no longer running
    (a superseded run must not become pending)."""
    values: dict[str, object] = {
        "status": AWAITING_CONFIRMATION,
        "installation_id": installation_id,
        "awaiting_since": datetime.now(UTC),
    }
    if held:  # a retry that does not know the count must not erase it
        values["held_count"] = held
    result = session.execute(
        update(ReviewRun)
        .where(ReviewRun.id == run_id, ReviewRun.status.in_((RUNNING, AWAITING_CONFIRMATION)))
        .values(**values)
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


def release_pending_comment(session: Session, run_id: str) -> None:
    """The 'pending' comment could not be posted: give the right back so it is tried again."""
    session.execute(
        update(ReviewRun)
        .where(ReviewRun.id == run_id, ReviewRun.status == AWAITING_CONFIRMATION)
        .values(pending_comment_posted=False)
    )
    session.commit()


def list_unannounced(session: Session) -> list[tuple[str, str, int, int, int]]:
    """Held runs nobody has been told about: (run_id, repo, pr_number, installation_id, held)."""
    rows = session.scalars(
        select(ReviewRun).where(
            ReviewRun.status == AWAITING_CONFIRMATION,
            ReviewRun.pending_comment_posted.is_(False),
            ReviewRun.installation_id.is_not(None),
        )
    ).all()
    return [(r.id, r.repo, r.pr_number, int(r.installation_id or 0), r.held_count) for r in rows]


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


# --- PR closed / merged ---------------------------------------------------------------------


def close_pr_runs(
    session: Session, repo: str, pr_number: int, updated_at: datetime | None = None
) -> tuple[int, int]:
    """The PR was closed or merged (no commit). Returns (held_expired, in_progress_superseded).

    - A held run becomes `expired` QUIETLY (no comment on a dead PR) and its checkpoint is queued
      for deletion. Nobody can /confirm it any more: confirmation only claims held runs.
    - A queued/running run is superseded: it must not spend more money or post.
    - A run stamped LATER than this event belongs to a push or reopen that the close preceded
      (deliveries can arrive out of order): it is left alone.
    - A `confirming` run is already posting; leave it.
    """
    conditions = [
        ReviewRun.repo == repo,
        ReviewRun.pr_number == pr_number,
    ]
    if updated_at is not None:
        conditions.append(
            or_(ReviewRun.head_updated_at.is_(None), ReviewRun.head_updated_at <= updated_at)
        )
    now = datetime.now(UTC)
    held = session.execute(
        update(ReviewRun)
        .where(*conditions, ReviewRun.status == AWAITING_CONFIRMATION)
        .values(status=EXPIRED, finished_at=now, expired_comment_posted=True, purge_checkpoint=True)
    )
    running = session.execute(
        update(ReviewRun)
        .where(*conditions, ReviewRun.status.in_((QUEUED, RUNNING)))
        .values(status=SUPERSEDED, finished_at=now)
    )
    return int(held.rowcount), int(running.rowcount)  # type: ignore[attr-defined]


def list_checkpoints_to_purge(session: Session) -> list[str]:
    return list(
        session.scalars(
            select(ReviewRun.id).where(
                ReviewRun.purge_checkpoint.is_(True), ReviewRun.status == EXPIRED
            )
        ).all()
    )


def mark_checkpoint_purged(session: Session, run_id: str) -> None:
    session.execute(update(ReviewRun).where(ReviewRun.id == run_id).values(purge_checkpoint=False))
    session.commit()


# --- /confirm refusals ------------------------------------------------------------------------


def is_denied(session: Session, run_id: str, login: str) -> bool:
    return session.get(ConfirmDenial, (run_id, login)) is not None


def denial_count(session: Session, run_id: str) -> int:
    return len(
        list(session.scalars(select(ConfirmDenial.login).where(ConfirmDenial.run_id == run_id)))
    )


def record_denial(session: Session, run_id: str, login: str) -> bool:
    """True if this login was newly refused (False: already on record)."""
    session.add(ConfirmDenial(run_id=run_id, login=login))
    try:
        session.commit()
    except IntegrityError:
        session.rollback()
        return False
    return True


def list_known_findings(
    session: Session, repo: str, pr_number: int, exclude_run_id: str
) -> list[tuple[str, str, int]]:
    """(file, category, line) of every finding that is visible on this PR from OTHER runs:
    posted, and not dismissed as a false positive."""
    rows = session.execute(
        select(FindingRow.file, FindingRow.category, FindingRow.line)
        .join(ReviewRun, ReviewRun.id == FindingRow.run_id)
        .where(
            ReviewRun.repo == repo,
            ReviewRun.pr_number == pr_number,
            ReviewRun.id != exclude_run_id,
            ReviewRun.posted_at.is_not(None),  # a review that really went out
            FindingRow.posted.is_(True),
            FindingRow.fixed_in_sha.is_(None),  # a fixed one that comes back is a NEW finding
        )
    ).all()
    return [(r.file, r.category, r.line) for r in rows]


def list_open_findings(
    session: Session, repo: str, pr_number: int, exclude_run_id: str
) -> list[tuple[int, str, str, int, int | None]]:
    """Findings visible on this PR from other runs and not yet fixed:
    (row id, file, category, line, GitHub comment id or None)."""
    rows = session.execute(
        select(
            FindingRow.id,
            FindingRow.file,
            FindingRow.category,
            FindingRow.line,
            FindingRow.github_comment_id,
        )
        .join(ReviewRun, ReviewRun.id == FindingRow.run_id)
        .where(
            ReviewRun.repo == repo,
            ReviewRun.pr_number == pr_number,
            ReviewRun.id != exclude_run_id,
            ReviewRun.posted_at.is_not(None),
            FindingRow.posted.is_(True),
            FindingRow.fixed_in_sha.is_(None),
            FindingRow.false_positive.is_(False),  # dismissed: nothing to say "fixed" about
        )
    ).all()
    return [(r.id, r.file, r.category, r.line, r.github_comment_id) for r in rows]


def claim_fixed(session: Session, finding_id: int, sha: str) -> bool:
    """Mark a finding fixed, atomically. True for exactly ONE caller: the right to reply."""
    result = session.execute(
        update(FindingRow)
        .where(FindingRow.id == finding_id, FindingRow.fixed_in_sha.is_(None))
        .values(fixed_in_sha=sha)
    )
    session.commit()
    return bool(result.rowcount)  # type: ignore[attr-defined]


def release_fixed(session: Session, finding_id: int) -> None:
    """The reply failed: forget the claim so a later push tries again."""
    session.execute(update(FindingRow).where(FindingRow.id == finding_id).values(fixed_in_sha=None))
    session.commit()


def save_github_ids(
    session: Session,
    run_id: str,
    *,
    review_id: int | None,
    comments: list[tuple[str, int, int, str | None]],
) -> None:
    """Attach GitHub's ids to the posted findings of a run. `comments` = (path, line, comment_id,
    node_id). A finding is matched by file and line; rows that already have an id are left alone.
    Returns how many findings got a comment id."""
    rows = list(
        session.scalars(
            select(FindingRow).where(
                FindingRow.run_id == run_id,
                FindingRow.posted.is_(True),
                FindingRow.github_comment_id.is_(None),
            )
        ).all()
    )
    for path, line, comment_id, node_id in comments:
        row = next((r for r in rows if r.file == path and r.line == line), None)
        if row is None:
            continue
        rows.remove(row)
        row.github_comment_id = comment_id
        row.github_node_id = node_id
        row.github_review_id = review_id
    if review_id is not None:
        for row in rows:  # posted in this review but not matched to a comment (summary fallback)
            row.github_review_id = row.github_review_id or review_id
    session.commit()


# --- /dismiss ----------------------------------------------------------------------------------


def dismiss_held(session: Session, run_id: str) -> bool:
    """awaiting_confirmation -> dismissed, atomically (a confirm that won keeps its run)."""
    result = session.execute(
        update(ReviewRun)
        .where(ReviewRun.id == run_id, ReviewRun.status == AWAITING_CONFIRMATION)
        .values(status=DISMISSED, finished_at=datetime.now(UTC), purge_checkpoint=True)
    )
    session.commit()
    return bool(result.rowcount)  # type: ignore[attr-defined]


def find_posted_finding_by_comment(
    session: Session, repo: str, pr_number: int, comment_id: int
) -> tuple[int, str] | None:
    """(finding id, run id) of the finding whose GitHub comment this is, on this PR."""
    row = session.execute(
        select(FindingRow.id, FindingRow.run_id)
        .join(ReviewRun, ReviewRun.id == FindingRow.run_id)
        .where(
            ReviewRun.repo == repo,
            ReviewRun.pr_number == pr_number,
            FindingRow.github_comment_id == comment_id,
        )
    ).first()
    return None if row is None else (row.id, row.run_id)


def dismiss_finding(session: Session, finding_id: int, login: str) -> bool:
    """Mark a finding a false positive, atomically. True for exactly ONE caller; the run's
    dismissed_count goes up with it."""
    result = session.execute(
        update(FindingRow)
        .where(FindingRow.id == finding_id, FindingRow.false_positive.is_(False))
        .values(false_positive=True, dismissed_by=login)
    )
    if not result.rowcount:  # type: ignore[attr-defined]
        session.commit()
        return False
    run_id = session.scalar(select(FindingRow.run_id).where(FindingRow.id == finding_id))
    session.execute(
        update(ReviewRun)
        .where(ReviewRun.id == run_id)
        .values(dismissed_count=ReviewRun.dismissed_count + 1)
    )
    session.commit()
    return True


def undo_dismiss(session: Session, finding_id: int) -> None:
    """The reply failed: take the dismissal back so the maintainer can try again."""
    run_id = session.scalar(select(FindingRow.run_id).where(FindingRow.id == finding_id))
    result = session.execute(
        update(FindingRow)
        .where(FindingRow.id == finding_id, FindingRow.false_positive.is_(True))
        .values(false_positive=False, dismissed_by=None)
    )
    if result.rowcount:  # type: ignore[attr-defined]
        session.execute(
            update(ReviewRun)
            .where(ReviewRun.id == run_id)
            .values(dismissed_count=ReviewRun.dismissed_count - 1)
        )
    session.commit()


def dismissal_rate(session: Session, run_id: str) -> float | None:
    """Share of a run's posted findings that maintainers dismissed (None: nothing was posted)."""
    posted = len(
        list(
            session.scalars(
                select(FindingRow.id).where(
                    FindingRow.run_id == run_id, FindingRow.posted.is_(True)
                )
            )
        )
    )
    run = session.get(ReviewRun, run_id)
    if not posted or run is None:
        return None
    return run.dismissed_count / posted


def claim_dismissed_comment(session: Session, run_id: str) -> bool:
    """True exactly once per dismissed run: the right to post the 'dismissed' comment."""
    result = session.execute(
        update(ReviewRun)
        .where(
            ReviewRun.id == run_id,
            ReviewRun.expired_comment_posted.is_(False),
            ReviewRun.status == DISMISSED,
        )
        .values(expired_comment_posted=True)
    )
    session.commit()
    return bool(result.rowcount)  # type: ignore[attr-defined]
