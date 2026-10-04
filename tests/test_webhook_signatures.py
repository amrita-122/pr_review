import hashlib
import hmac
import json
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

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


@pytest.mark.parametrize("action", ["reopened", "synchronize"])
def test_other_handled_actions_accepted(action: str) -> None:
    assert post_signed(body_with_action(action)).json() == {"status": "accepted"}


@pytest.mark.parametrize("action", ["closed", "labeled", "edited"])
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
