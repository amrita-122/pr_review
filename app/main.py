from collections.abc import Iterator

from fastapi import BackgroundTasks, Depends, FastAPI, Header, HTTPException, Request
from pydantic import ValidationError
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db.deliveries import record_delivery
from app.db.session import get_session
from app.github.webhooks import (
    HANDLED_ACTIONS,
    HANDLED_EVENT,
    parse_pull_request_event,
    verify_signature,
)
from app.jobs.handlers import process_pull_request

app = FastAPI()


def get_db() -> Iterator[Session]:
    with get_session() as session:
        yield session


@app.get("/health")
def health_check() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/webhooks/github", status_code=202)
async def handle_webhook(
    request: Request,
    background_tasks: BackgroundTasks,
    session: Session = Depends(get_db),  # noqa: B008
    signature: str | None = Header(None, alias="X-Hub-Signature-256"),
    event: str | None = Header(None, alias="X-GitHub-Event"),
    delivery_id: str | None = Header(None, alias="X-GitHub-Delivery"),
) -> dict[str, str]:
    settings = get_settings()
    secret = settings.github_webhook_secret.get_secret_value()
    body = await request.body()
    if not verify_signature(secret, body, signature):
        raise HTTPException(status_code=401)

    if event != HANDLED_EVENT:
        return {"status": "ignored"}

    try:
        pr_event = parse_pull_request_event(body)
    except ValidationError:
        raise HTTPException(status_code=422, detail="Invalid payload") from None

    if pr_event.action not in HANDLED_ACTIONS:
        return {"status": "ignored"}

    if delivery_id is None:
        raise HTTPException(status_code=400, detail="Missing X-GitHub-Delivery")

    # Insert first: the primary key makes redeliveries a no-op.
    if not record_delivery(session, delivery_id, event, pr_event.action):
        return {"status": "duplicate"}

    background_tasks.add_task(process_pull_request, pr_event)
    return {"status": "accepted"}
