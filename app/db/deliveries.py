from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db.models import WebhookDelivery


def record_delivery(session: Session, delivery_id: str, event: str, action: str | None) -> bool:
    """Insert the delivery id. Returns False if it was already recorded (a redelivery)."""
    session.add(WebhookDelivery(delivery_id=delivery_id, event=event, action=action))
    try:
        session.commit()
    except IntegrityError:
        session.rollback()
        return False
    return True
