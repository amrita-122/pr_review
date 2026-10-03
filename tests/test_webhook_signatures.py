import hashlib
import hmac
from pathlib import Path

from fastapi.testclient import TestClient

from app.config import get_settings
from app.main import app

client = TestClient(app)

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "pull_request_opened.json"

TEST_SECRET = "test-webhook-secret"  # noqa: S105


def make_signature(secret: str, body: bytes) -> str:
    digest = hmac.new(
        secret.encode(),
        body,
        hashlib.sha256,
    ).hexdigest()

    return f"sha256={digest}"


def test_valid_signature(monkeypatch):
    body = FIXTURE_PATH.read_bytes()
    signature = make_signature(TEST_SECRET, body)

    # Adjust this depending on how your settings are mocked in your project.
    monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", TEST_SECRET)
    get_settings.cache_clear()

    response = client.post(
        "/webhooks/github",
        content=body,
        headers={"X-Hub-Signature-256": signature},
    )

    assert 200 <= response.status_code < 300


def test_wrong_signature(monkeypatch):
    body = FIXTURE_PATH.read_bytes()

    monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", TEST_SECRET)
    get_settings.cache_clear()

    response = client.post(
        "/webhooks/github",
        content=body,
        headers={"X-Hub-Signature-256": "sha256=wrong"},
    )

    assert response.status_code == 401


def test_missing_signature(monkeypatch):
    body = FIXTURE_PATH.read_bytes()

    monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", TEST_SECRET)
    get_settings.cache_clear()

    response = client.post(
        "/webhooks/github",
        content=body,
    )

    assert response.status_code == 401


def test_tampered_body(monkeypatch):
    body = FIXTURE_PATH.read_bytes()
    signature = make_signature(TEST_SECRET, body)

    tampered_body = body + b" "

    monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", TEST_SECRET)
    get_settings.cache_clear()

    response = client.post(
        "/webhooks/github",
        content=tampered_body,
        headers={"X-Hub-Signature-256": signature},
    )

    assert response.status_code == 401
