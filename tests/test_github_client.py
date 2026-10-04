import base64
import json
from datetime import UTC, datetime, timedelta

import httpx
import jwt
import pytest
import respx
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from app.config import get_settings
from app.github import auth
from app.github.client import post_pr_comment
from app.github.webhooks import parse_pull_request_event
from app.jobs.handlers import process_pull_request
from tests.test_webhook_signatures import FIXTURE_PATH

_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
PRIVATE_PEM = _key.private_bytes(
    serialization.Encoding.PEM,
    serialization.PrivateFormat.PKCS8,
    serialization.NoEncryption(),
).decode()
PUBLIC_PEM = _key.public_key().public_bytes(
    serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
)


@pytest.fixture(autouse=True)
def github_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GITHUB_APP_ID", "12345")
    monkeypatch.setenv("GITHUB_PRIVATE_KEY_BASE64", base64.b64encode(PRIVATE_PEM.encode()).decode())
    get_settings.cache_clear()
    auth._token_cache.clear()
    yield
    get_settings.cache_clear()
    auth._token_cache.clear()


def _token_response(minutes: int = 60) -> httpx.Response:
    expires = (datetime.now(UTC) + timedelta(minutes=minutes)).strftime("%Y-%m-%dT%H:%M:%SZ")
    return httpx.Response(201, json={"token": "ghs_tok", "expires_at": expires})


def test_app_jwt_claims() -> None:
    token = auth.build_app_jwt("12345", PRIVATE_PEM, now=1_000_000)
    claims = jwt.decode(token, PUBLIC_PEM, algorithms=["RS256"], options={"verify_exp": False})
    assert claims == {"iat": 1_000_000 - 60, "exp": 1_000_000 + 540, "iss": "12345"}


@respx.mock
async def test_token_exchange_and_cache() -> None:
    route = respx.post("https://api.github.com/app/installations/7/access_tokens").mock(
        return_value=_token_response()
    )
    assert await auth.get_installation_token(7) == "ghs_tok"
    assert await auth.get_installation_token(7) == "ghs_tok"
    assert route.call_count == 1
    assert route.calls[0].request.headers["Authorization"].startswith("Bearer ")


@respx.mock
async def test_token_refreshed_when_near_expiry() -> None:
    route = respx.post("https://api.github.com/app/installations/7/access_tokens").mock(
        return_value=_token_response(minutes=2)
    )
    await auth.get_installation_token(7)
    await auth.get_installation_token(7)
    assert route.call_count == 2


@respx.mock
async def test_post_pr_comment() -> None:
    route = respx.post("https://api.github.com/repos/o/r/issues/3/comments").mock(
        return_value=httpx.Response(201, json={})
    )
    await post_pr_comment("tok", "o", "r", 3, "hello")
    request = route.calls[0].request
    assert json.loads(request.content) == {"body": "hello"}
    assert request.headers["Authorization"] == "Bearer tok"


@respx.mock
async def test_process_pull_request_posts_comment() -> None:
    event = parse_pull_request_event(FIXTURE_PATH.read_bytes())
    respx.post(
        f"https://api.github.com/app/installations/{event.installation_id}/access_tokens"
    ).mock(return_value=_token_response())
    comment = respx.post(
        f"https://api.github.com/repos/{event.owner}/{event.repo}/issues/{event.number}/comments"
    ).mock(return_value=httpx.Response(201, json={}))
    await process_pull_request(event)
    assert comment.called


@respx.mock
async def test_process_pull_request_swallows_errors() -> None:
    event = parse_pull_request_event(FIXTURE_PATH.read_bytes())
    respx.post(
        f"https://api.github.com/app/installations/{event.installation_id}/access_tokens"
    ).mock(return_value=httpx.Response(500))
    await process_pull_request(event)  # must not raise
