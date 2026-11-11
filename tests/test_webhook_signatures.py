import hashlib
import hmac
import json
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.github.webhooks import parse_pull_request_event
from app.main import app
from tests.conftest import TEST_SECRET

client = TestClient(app)

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "pull_request_opened.json"


def make_signature(secret: str, body: bytes) -> str:
    digest = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def post_signed(  # noqa: ANN201
    body: bytes, event: str | None = "pull_request", delivery: str | None = None
):
    headers = {
        "X-Hub-Signature-256": make_signature(TEST_SECRET, body),
        "X-GitHub-Delivery": delivery or str(uuid.uuid4()),
    }
    if event is not None:
        headers["X-GitHub-Event"] = event
    return client.post("/webhooks/github", content=body, headers=headers)


def body_with_action(action: str) -> bytes:
    payload = json.loads(FIXTURE_PATH.read_bytes())
    payload["action"] = action
    return json.dumps(payload).encode()


# --- signature (1.6) ---


def test_valid_signature() -> None:
    response = post_signed(FIXTURE_PATH.read_bytes())
    assert response.status_code == 202


def test_wrong_signature() -> None:
    response = client.post(
        "/webhooks/github",
        content=FIXTURE_PATH.read_bytes(),
        headers={"X-Hub-Signature-256": "sha256=wrong", "X-GitHub-Event": "pull_request"},
    )
    assert response.status_code == 401


def test_missing_signature() -> None:
    response = client.post("/webhooks/github", content=FIXTURE_PATH.read_bytes())
    assert response.status_code == 401


def test_tampered_body() -> None:
    body = FIXTURE_PATH.read_bytes()
    response = client.post(
        "/webhooks/github",
        content=body + b" ",
        headers={
            "X-Hub-Signature-256": make_signature(TEST_SECRET, body),
            "X-GitHub-Event": "pull_request",
        },
    )
    assert response.status_code == 401


# --- event / action filtering (1.7) ---


def test_opened_is_accepted() -> None:
    response = post_signed(body_with_action("opened"))
    assert response.status_code == 202
    assert response.json() == {"status": "accepted"}


@pytest.mark.parametrize("action", ["reopened", "synchronize", "ready_for_review"])
def test_other_handled_actions_accepted(action: str) -> None:
    assert post_signed(body_with_action(action)).json() == {"status": "accepted"}


@pytest.mark.parametrize("action", ["labeled", "edited"])
def test_unhandled_action_ignored(action: str) -> None:
    response = post_signed(body_with_action(action))
    assert response.status_code == 202
    assert response.json() == {"status": "ignored"}


def test_non_pull_request_event_ignored() -> None:
    response = post_signed(b'{"zen": "hi"}', event="ping")
    assert response.status_code == 202
    assert response.json() == {"status": "ignored"}


def test_missing_event_header_ignored() -> None:
    assert post_signed(FIXTURE_PATH.read_bytes(), event=None).json() == {"status": "ignored"}


def test_malformed_json_with_valid_signature_is_422() -> None:
    assert post_signed(b"not json").status_code == 422


def test_missing_required_field_is_422() -> None:
    assert post_signed(b'{"action": "opened"}').status_code == 422


def test_parse_pull_request_event_fields() -> None:
    event = parse_pull_request_event(FIXTURE_PATH.read_bytes())
    assert event.action == "opened"
    assert event.number == 1
    assert event.installation_id == 167422240
    assert event.owner == "amrita-122"
    assert event.repo == "review-bot-sandbox"
    assert event.head_sha == "769c20f67155f2c7ddcfa3d1bd6919b3856786ff"


# --- drafts (10.4) ---


def draft_body(action: str = "opened") -> bytes:
    payload = json.loads(body_with_action(action))
    payload["pull_request"]["draft"] = True
    return json.dumps(payload).encode()


@pytest.mark.parametrize("action", ["opened", "reopened", "synchronize"])
def test_draft_pr_is_ignored_and_queues_nothing(action: str, db_session: Session) -> None:
    from sqlalchemy import func, select

    from app.db.models import Job, ReviewRun, WebhookDelivery

    assert post_signed(draft_body(action)).json() == {"status": "ignored"}
    for model in (Job, ReviewRun, WebhookDelivery):
        assert db_session.scalar(select(func.count()).select_from(model)) == 0


def test_marking_a_draft_ready_for_review_starts_the_review(db_session: Session) -> None:
    from sqlalchemy import func, select

    from app.db.models import Job

    assert post_signed(draft_body()).json() == {"status": "ignored"}
    assert post_signed(body_with_action("ready_for_review")).json() == {"status": "accepted"}
    assert db_session.scalar(select(func.count()).select_from(Job)) == 1


def test_review_drafts_setting_turns_the_skip_off(
    monkeypatch: pytest.MonkeyPatch, db_session: Session
) -> None:
    from app.config import get_settings

    monkeypatch.setenv("REVIEW_DRAFTS", "true")
    get_settings.cache_clear()
    assert post_signed(draft_body()).json() == {"status": "accepted"}


def test_payload_without_a_draft_field_is_treated_as_ready() -> None:
    payload = json.loads(body_with_action("opened"))
    del payload["pull_request"]["draft"]
    assert post_signed(json.dumps(payload).encode()).json() == {"status": "accepted"}
