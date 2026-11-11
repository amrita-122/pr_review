from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager

import structlog
from fastapi import Depends, FastAPI, Header, HTTPException, Request
from pydantic import ValidationError
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import runs
from app.db.deliveries import record_delivery
from app.db.session import get_session
from app.github.webhooks import (
    CLOSED_ACTION,
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

logger = structlog.get_logger()

# After this many different logins were refused on one held review, further /confirm comments are
# ignored without a lookup. (A maintainer can still push a new commit to start over.)
MAX_DENIED_LOGINS_PER_RUN = 10


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


async def _read_capped_body(request: Request, limit: int) -> bytes:
    """The raw body, refusing (413) anything over `limit` BEFORE it is all in memory or hashed.
    The declared Content-Length is checked first, then the bytes actually received."""
    declared = request.headers.get("content-length")
    if declared is not None and declared.isdigit() and int(declared) > limit:
        raise HTTPException(status_code=413, detail="Payload too large")
    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > limit:
            raise HTTPException(status_code=413, detail="Payload too large")
        chunks.append(chunk)
    return b"".join(chunks)


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
    body = await _read_capped_body(request, settings.max_webhook_body_bytes)
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

    if pr_event.action not in HANDLED_ACTIONS and pr_event.action != CLOSED_ACTION:
        return {"status": "ignored"}

    if pr_event.is_draft and pr_event.action in HANDLED_ACTIONS and not settings.review_drafts:
        # Not recorded: marking it ready for review arrives as its own event and is handled then.
        return {"status": "ignored"}

    if delivery_id is None:
        raise HTTPException(status_code=400, detail="Missing X-GitHub-Delivery")

    # Insert first: the primary key makes redeliveries a no-op.
    if not record_delivery(session, delivery_id, event, pr_event.action):
        return {"status": "duplicate"}

    if pr_event.action == CLOSED_ACTION:
        # A dead PR gets no more reviews, comments or confirmations. Nothing is queued.
        repo = f"{pr_event.owner}/{pr_event.repo}"
        runs.close_pr_runs(session, repo, pr_event.number, pr_event.head_updated_at)
        session.commit()
        return {"status": "closed"}

    # Delivery + run + job are committed together: a crash can never record a delivery whose
    # review job was lost. If this raises, nothing is saved and GitHub's redelivery works.
    try:
        enqueue_review(session, pr_event, settings)
        session.commit()
    except IntegrityError:
        # Two deliveries for the same commit raced: both saw no run, the other one inserted it
        # first (unique repo/PR/sha). The review is queued by the winner, so nothing is lost.
        session.rollback()
        logger.info("concurrent_delivery_for_same_commit", delivery_id=delivery_id)
        record_delivery(session, delivery_id, event, pr_event.action)  # keep redeliveries no-ops
        session.commit()
        return {"status": "duplicate"}
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
    # Already refused once, or too many different people refused: costs nothing, looks up nothing.
    if runs.is_denied(session, held.id, comment_event.login) or (
        runs.denial_count(session, held.id) >= MAX_DENIED_LOGINS_PER_RUN
    ):
        return {"status": "ignored"}

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
        author_login=comment_event.author_login,
    )
    enqueue_confirm(session, payload, settings)
    session.commit()  # delivery + job together, like a review
    return {"status": "accepted"}
