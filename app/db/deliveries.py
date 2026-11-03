from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db.models import WebhookDelivery


def record_delivery(session: Session, delivery_id: str, event: str, action: str | None) -> bool:
    """Insert the delivery id (flush, not commit). False if it was already recorded (a redelivery).

    The caller commits, so the delivery and the job it triggers are saved atomically.
    """
    session.add(WebhookDelivery(delivery_id=delivery_id, event=event, action=action))
    try:
        session.flush()
    except IntegrityError:
        session.rollback()
        return False
    return True
