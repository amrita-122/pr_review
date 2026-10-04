from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.models import WebhookDelivery
from app.github.webhooks import PullRequestEvent
from tests.test_webhook_signatures import (
    FIXTURE_PATH,
    TEST_SECRET,
    client,
    make_signature,
    post_signed,
)


def test_same_delivery_processed_once(
    db_session: Session, processed: list[PullRequestEvent]
) -> None:
    body = FIXTURE_PATH.read_bytes()

    first = post_signed(body, delivery="abc-123")
    second = post_signed(body, delivery="abc-123")

    assert first.status_code == second.status_code == 202
    assert first.json() == {"status": "accepted"}
    assert second.json() == {"status": "duplicate"}
    assert len(processed) == 1
    assert db_session.scalar(select(func.count()).select_from(WebhookDelivery)) == 1


def test_different_deliveries_both_processed(processed: list[PullRequestEvent]) -> None:
    body = FIXTURE_PATH.read_bytes()
    post_signed(body, delivery="one")
    post_signed(body, delivery="two")
    assert len(processed) == 2


def test_ignored_actions_do_not_run_job(processed: list[PullRequestEvent]) -> None:
    assert post_signed(b'{"zen": "hi"}', event="ping").json() == {"status": "ignored"}
    assert processed == []


def test_missing_delivery_header_is_400() -> None:
    body = FIXTURE_PATH.read_bytes()
    response = client.post(
        "/webhooks/github",
        content=body,
        headers={
            "X-Hub-Signature-256": make_signature(TEST_SECRET, body),
            "X-GitHub-Event": "pull_request",
        },
    )
    assert response.status_code == 400
