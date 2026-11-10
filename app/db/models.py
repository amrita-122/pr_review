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
        # list_expired / list_unannounced / confirm lookups all filter on these two columns
        Index("ix_review_runs_status_awaiting_since", "status", "awaiting_since"),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True)
    repo: Mapped[str] = mapped_column(String)
    pr_number: Mapped[int] = mapped_column(Integer)
    head_sha: Mapped[str] = mapped_column(String)
    # queued | running | ok | failed | superseded (a newer push replaced it; it must not post)
    # | awaiting_confirmation | confirming | expired (Phase 8)
    status: Mapped[str] = mapped_column(String)
    # The webhook's pull_request.updated_at for this head: orders pushes that arrive out of order.
    head_updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
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
    # The second review, posted after /confirm, when the non-held findings already went out (10.8).
    held_posted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    failure_comment_posted: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=false()
    )
    # Phase 8 (human confirmation). Statuses added: awaiting_confirmation | confirming | expired.
    installation_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)  # set when held
    awaiting_since: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    pending_comment_posted: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=false()
    )
    expired_comment_posted: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=false()
    )
    # Set when the PR closed while this run was held: its checkpoint (it holds source code) must go.
    # How many findings are held. Kept so the 'pending' comment can be re-sent after a failed POST.
    held_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    purge_checkpoint: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())
    confirmed_by: Mapped[str | None] = mapped_column(String, nullable=True)  # GitHub login
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


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
    # Where GitHub put it (10.14): needed to reply to it, resolve its thread, or recognise it later.
    github_review_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    github_comment_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    github_node_id: Mapped[str | None] = mapped_column(String, nullable=True)
    # Set when a later push no longer reproduces this finding (10.16): the commit that fixed it.
    fixed_in_sha: Mapped[str | None] = mapped_column(String, nullable=True)


class ConfirmDenial(Base):
    """A login that tried /confirm on a held run and was refused. One row per (run, login): the
    webhook ignores repeats for free, so comment spam costs no permission lookups."""

    __tablename__ = "confirm_denials"

    run_id: Mapped[str] = mapped_column(ForeignKey("review_runs.id"), primary_key=True)
    login: Mapped[str] = mapped_column(String, primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


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
