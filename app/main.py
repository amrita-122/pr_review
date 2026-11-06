from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from pydantic import ValidationError
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import runs
from app.db.deliveries import record_delivery
from app.db.session import get_session
from app.github.webhooks import (
    HANDLED_ACTIONS,
    HANDLED_EVENT,
    ISSUE_COMMENT_EVENT,
    parse_issue_comment_event,
    parse_pull_request_event,
    verify_signature,
)
from app.jobs.payload import ConfirmJobPayload
from app.jobs.queue import enqueue_confirm, enqueue_review
from app.logging import configure_logging
from app.observability import configure_tracing


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    configure_logging()
    configure_tracing(get_settings())
    yield


app = FastAPI(lifespan=lifespan)


def get_db() -> Iterator[Session]:
    with get_session() as session:
        yield session


@app.get("/health")
def health_check() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/webhooks/github", status_code=202)
async def handle_webhook(
    request: Request,
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

    if event == ISSUE_COMMENT_EVENT:
        return _handle_issue_comment(session, body, delivery_id)

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

    # Delivery + run + job are committed together: a crash can never record a delivery whose
    # review job was lost. If this raises, nothing is saved and GitHub's redelivery works.
    enqueue_review(session, pr_event, settings)
    session.commit()
    return {"status": "accepted"}


def _handle_issue_comment(session: Session, body: bytes, delivery_id: str | None) -> dict[str, str]:
    """A PR comment that may be a maintainer's /confirm. The comment is untrusted input.

    Only "who" and "which PR" are taken from it. Whether that person may confirm is decided later,
    by the worker, with a GitHub permission lookup. Anything that is not a plausible confirm for a
    PR with a held review is ignored BEFORE any row is written, so comment spam costs nothing.
    """
    settings = get_settings()
    try:
        comment_event = parse_issue_comment_event(body)
    except ValidationError:
        raise HTTPException(status_code=422, detail="Invalid payload") from None

    if not settings.confirmation_enabled or not comment_event.is_confirm_request:
        return {"status": "ignored"}
    if delivery_id is None:
        raise HTTPException(status_code=400, detail="Missing X-GitHub-Delivery")

    repo = f"{comment_event.owner}/{comment_event.repo}"
    held = runs.get_awaiting_for_pr(session, repo, comment_event.number)
    if held is None:
        return {"status": "ignored"}  # not recorded: a later redelivery may still be useful

    if not record_delivery(session, delivery_id, ISSUE_COMMENT_EVENT, comment_event.action):
        return {"status": "duplicate"}
    payload = ConfirmJobPayload(
        run_id=held.id,
        installation_id=comment_event.installation_id,
        owner=comment_event.owner,
        repo=comment_event.repo,
        number=comment_event.number,
        comment_id=comment_event.comment.id,
        commenter_login=comment_event.login,
    )
    enqueue_confirm(session, payload, settings)
    session.commit()  # delivery + job together, like a review
    return {"status": "accepted"}
