"""Persistence for review runs and findings. Callers treat failures here as non-fatal."""

from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy.orm import Session

from app.db.models import FindingRow, ReviewRun
from app.review.schemas import Finding

MAX_ERROR_CHARS = 500


def start_run(
    session: Session,
    *,
    run_id: str,
    repo: str,
    pr_number: int,
    head_sha: str,
    prompt_version: str,
    model: str,
) -> None:
    session.add(
        ReviewRun(
            id=run_id,
            repo=repo,
            pr_number=pr_number,
            head_sha=head_sha,
            status="running",
            prompt_version=prompt_version,
            model=model,
        )
    )
    session.commit()


def finish_run(
    session: Session,
    run_id: str,
    *,
    status: str,
    tokens_in: int = 0,
    tokens_out: int = 0,
    cost_usd: Decimal = Decimal(0),
    error: str | None = None,
) -> None:
    run = session.get(ReviewRun, run_id)
    if run is None:
        return
    run.status = status
    run.finished_at = datetime.now(UTC)
    run.tokens_in = tokens_in
    run.tokens_out = tokens_out
    run.cost_usd = cost_usd
    # Exception type + short message only: never a stack trace (may hold URLs or secrets).
    run.error = error[:MAX_ERROR_CHARS] if error else None
    session.commit()


def save_findings(
    session: Session,
    run_id: str,
    posted: list[Finding],
    dropped: list[tuple[Finding, str]],
) -> None:
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
