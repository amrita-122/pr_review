import base64
import time
from datetime import UTC, datetime

import httpx
import jwt

from app.config import get_settings

GITHUB_API = "https://api.github.com"
REFRESH_MARGIN_SECONDS = 300

# installation_id -> (token, expires_at)
_token_cache: dict[int, tuple[str, datetime]] = {}


def build_app_jwt(app_id: str, private_key_pem: str, now: int | None = None) -> str:
    """App JWT: RS256, iat 60s in the past (clock drift), exp well under GitHub's 10 min cap."""
    issued = int(time.time()) if now is None else now
    payload = {"iat": issued - 60, "exp": issued + 540, "iss": app_id}
    return jwt.encode(payload, private_key_pem, algorithm="RS256")


def _load_private_key() -> str:
    encoded = get_settings().github_private_key_base64.get_secret_value()
    return base64.b64decode(encoded).decode()


async def get_installation_token(installation_id: int) -> str:
    cached = _token_cache.get(installation_id)
    if cached is not None:
        token, expires_at = cached
        if (expires_at - datetime.now(UTC)).total_seconds() > REFRESH_MARGIN_SECONDS:
            return token

    app_jwt = build_app_jwt(get_settings().github_app_id, _load_private_key())
    async with httpx.AsyncClient(base_url=GITHUB_API, timeout=10) as client:
        response = await client.post(
            f"/app/installations/{installation_id}/access_tokens",
            headers={
                "Authorization": f"Bearer {app_jwt}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )
    response.raise_for_status()
    data = response.json()
    token = data["token"]
    _token_cache[installation_id] = (token, datetime.fromisoformat(data["expires_at"]))
    return str(token)
