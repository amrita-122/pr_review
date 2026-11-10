"""Housekeeping: old webhook deliveries and finished jobs do not need to live forever."""

from datetime import datetime, timedelta

import structlog
from sqlalchemy import delete
from sqlalchemy.orm import Session

from app.config import Settings
from app.db.models import Job, WebhookDelivery

logger = structlog.get_logger()
FINISHED_JOB_STATES = ("done", "failed")


def prune_old_rows(session: Session, now: datetime, settings: Settings) -> tuple[int, int]:
    """Delete deliveries and finished jobs past their retention. Returns (deliveries, jobs).

    Queued and running jobs are never touched, whatever their age. A delivery id deleted here
    could be processed again if GitHub redelivered it that late; the retention is far longer than
    GitHub keeps redelivery possible.
    """
    deliveries = session.execute(
        delete(WebhookDelivery).where(
            WebhookDelivery.received_at < now - timedelta(days=settings.retention_deliveries_days)
        )
    )
    jobs = session.execute(
        delete(Job).where(
            Job.status.in_(FINISHED_JOB_STATES),
            Job.created_at < now - timedelta(days=settings.retention_jobs_days),
        )
    )
    session.commit()
    counts = (int(deliveries.rowcount), int(jobs.rowcount))  # type: ignore[attr-defined]
    if any(counts):
        logger.info("old_rows_pruned", deliveries=counts[0], jobs=counts[1])
    return counts
