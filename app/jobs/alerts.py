"""Failure alerting (10.34): one short POST to an operator-owned webhook (Slack-style `text`).

The URL is a secret and the message is static text plus the run id and failure class: nothing from
the PR (no title, file, finding or repo content) ever goes into it. Best effort: an alert that
cannot be sent is logged and never fails the job.
"""

import httpx
import structlog

from app.config import Settings

logger = structlog.get_logger()
ALERT_TIMEOUT_SECONDS = 5.0


async def send_alert(settings: Settings, text: str) -> bool:
    if settings.alert_webhook_url is None:
        return False
    url = settings.alert_webhook_url.get_secret_value()
    try:
        async with httpx.AsyncClient(timeout=ALERT_TIMEOUT_SECONDS) as client:
            response = await client.post(url, json={"text": text[:500]})
        response.raise_for_status()
    except Exception as exc:  # never include the URL: it is a secret
        logger.warning("alert_not_sent", error=type(exc).__name__)
        return False
    return True
