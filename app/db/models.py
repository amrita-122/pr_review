from datetime import datetime
from decimal import Decimal

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, Numeric, String, Text, func
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
    """One review of one PR head. id is the run_id bound on every log line and trace."""

    __tablename__ = "review_runs"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    repo: Mapped[str] = mapped_column(String)
    pr_number: Mapped[int] = mapped_column(Integer)
    head_sha: Mapped[str] = mapped_column(String)
    status: Mapped[str] = mapped_column(String)  # running | ok | failed
    prompt_version: Mapped[str] = mapped_column(String)
    model: Mapped[str] = mapped_column(String)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    tokens_in: Mapped[int] = mapped_column(Integer, default=0)
    tokens_out: Mapped[int] = mapped_column(Integer, default=0)
    cost_usd: Mapped[Decimal] = mapped_column(Numeric(12, 6), default=Decimal(0))
    error: Mapped[str | None] = mapped_column(Text, nullable=True)  # type + short message only


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
