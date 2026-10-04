from datetime import datetime

from sqlalchemy import DateTime, String, func
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
