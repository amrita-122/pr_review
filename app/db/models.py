from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    false,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class WebhookDelivery(Base):
    """One row per X-GitHub-Delivery id; the primary key is what makes webhooks idempotent."""

    __tablename__ = "webhook_deliveries"

    delivery_id: Mapped[str] = mapped_column(String, primary_key=True)
    event: Mapped[str] = mapped_column(String)
    action: Mapped[str | None] = mapped_column(String, nullable=True)
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class ReviewRun(Base):
    """One review of one PR head. id is the run_id bound on every log line and trace.

    (repo, pr_number, head_sha) is unique: a retried job reuses its run instead of creating another.
    """

    __tablename__ = "review_runs"
    __table_args__ = (
        UniqueConstraint("repo", "pr_number", "head_sha", name="uq_review_runs_pr_head"),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True)
    repo: Mapped[str] = mapped_column(String)
    pr_number: Mapped[int] = mapped_column(Integer)
    head_sha: Mapped[str] = mapped_column(String)
    # queued | running | ok | failed | superseded (a newer push replaced it; it must not post)
    status: Mapped[str] = mapped_column(String)
    prompt_version: Mapped[str] = mapped_column(String)
    model: Mapped[str] = mapped_column(String)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    tokens_in: Mapped[int] = mapped_column(Integer, default=0)
    tokens_out: Mapped[int] = mapped_column(Integer, default=0)
    cost_usd: Mapped[Decimal] = mapped_column(Numeric(12, 6), default=Decimal(0))
    error: Mapped[str | None] = mapped_column(Text, nullable=True)  # type + short message only
    failure_reason: Mapped[str | None] = mapped_column(
        String, nullable=True
    )  # timeout|github|model|internal
    posted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    failure_comment_posted: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=false()
    )


class FindingRow(Base):
    """A finding the model produced (model output: untrusted text). posted=False means dropped."""

    __tablename__ = "findings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("review_runs.id"), index=True)
    file: Mapped[str] = mapped_column(String)
    line: Mapped[int] = mapped_column(Integer)
    severity: Mapped[str] = mapped_column(String)
    category: Mapped[str] = mapped_column(String)
    title: Mapped[str] = mapped_column(String)
    posted: Mapped[bool] = mapped_column(Boolean)
    drop_reason: Mapped[str | None] = mapped_column(String, nullable=True)


class Job(Base):
    """A unit of background work. Claimed with FOR UPDATE SKIP LOCKED (app/jobs/worker.py).

    payload holds only the few validated fields the job needs, never a raw webhook body.
    """

    __tablename__ = "jobs"
    __table_args__ = (Index("ix_jobs_status_run_after", "status", "run_after"),)

    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True
    )
    kind: Mapped[str] = mapped_column(String)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON().with_variant(JSONB(), "postgresql"))
    status: Mapped[str] = mapped_column(String, default="queued")  # queued|running|done|failed
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, default=3)
    run_after: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    locked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    locked_by: Mapped[str | None] = mapped_column(String, nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)  # type + status only
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
