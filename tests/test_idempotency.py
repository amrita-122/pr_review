import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import main as main_module
from app.db.models import Job, ReviewRun, WebhookDelivery
from tests.test_webhook_signatures import (
    FIXTURE_PATH,
    TEST_SECRET,
    client,
    make_signature,
    post_signed,
)


def test_same_delivery_processed_once(db_session: Session) -> None:
    body = FIXTURE_PATH.read_bytes()

    first = post_signed(body, delivery="abc-123")
    second = post_signed(body, delivery="abc-123")

    assert first.status_code == second.status_code == 202
    assert first.json() == {"status": "accepted"}
    assert second.json() == {"status": "duplicate"}
    assert db_session.scalar(select(func.count()).select_from(Job)) == 1
    assert db_session.scalar(select(func.count()).select_from(WebhookDelivery)) == 1


def test_different_deliveries_for_the_same_commit_make_one_job(db_session: Session) -> None:
    # Two deliveries (e.g. opened, then reopened) for one commit: the review runs once.
    body = FIXTURE_PATH.read_bytes()
    post_signed(body, delivery="one")
    second = post_signed(body, delivery="two")
    assert second.status_code == 202
    assert db_session.scalar(select(func.count()).select_from(WebhookDelivery)) == 2
    assert db_session.scalar(select(func.count()).select_from(Job)) == 1
    assert db_session.scalar(select(func.count()).select_from(ReviewRun)) == 1


def test_ignored_actions_do_not_queue_a_job(db_session: Session) -> None:
    assert post_signed(b'{"zen": "hi"}', event="ping").json() == {"status": "ignored"}
    assert db_session.scalar(select(func.count()).select_from(Job)) == 0


def test_delivery_and_job_are_saved_together_or_not_at_all(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    # If queueing fails after the delivery was recorded, NOTHING is saved, so a redelivery works.
    def boom(*_: object) -> None:
        raise RuntimeError("database hiccup")

    real = main_module.enqueue_review
    monkeypatch.setattr("app.main.enqueue_review", boom)
    body = FIXTURE_PATH.read_bytes()
    with pytest.raises(RuntimeError):
        post_signed(body, delivery="lost-1")
    assert db_session.scalar(select(func.count()).select_from(WebhookDelivery)) == 0
    monkeypatch.setattr("app.main.enqueue_review", real)  # GitHub's redelivery now succeeds
    assert post_signed(body, delivery="lost-1").json() == {"status": "accepted"}
    assert db_session.scalar(select(func.count()).select_from(Job)) == 1


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
