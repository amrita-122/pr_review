"""Shared test helpers: a throwaway GitHub App key and canned GitHub responses."""

import json
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import respx
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from app.github.webhooks import PullRequestEvent

_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
PRIVATE_PEM = _key.private_bytes(
    serialization.Encoding.PEM,
    serialization.PrivateFormat.PKCS8,
    serialization.NoEncryption(),
).decode()
PUBLIC_PEM = _key.public_key().public_bytes(
    serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
)

FILES = [
    {
        "filename": "app/db.py",
        "status": "modified",
        "patch": "@@ -1,2 +1,3 @@\n import os\n+password = 'hunter2'\n print(1)",
    },
    {"filename": "uv.lock", "status": "modified", "patch": "@@ -1 +1 @@\n-a\n+b"},
    {"filename": "logo.png", "status": "added"},
]


def token_response(minutes: int = 60) -> httpx.Response:
    expires = (datetime.now(UTC) + timedelta(minutes=minutes)).strftime("%Y-%m-%dT%H:%M:%SZ")
    return httpx.Response(201, json={"token": "ghs_tok", "expires_at": expires})


class Pipeline:
    """The GitHub endpoints one review touches, mocked with respx. Re-mock a route to steer."""

    def __init__(self, event: PullRequestEvent, *, head_sha: str | None = None) -> None:
        repo_url = f"https://api.github.com/repos/{event.owner}/{event.repo}"
        self.base = f"{repo_url}/pulls/{event.number}"
        self.token = respx.post(
            f"https://api.github.com/app/installations/{event.installation_id}/access_tokens"
        ).mock(return_value=token_response())
        self.files = respx.get(f"{self.base}/files").mock(
            return_value=httpx.Response(200, json=FILES)
        )
        self.head = respx.get(self.base).mock(
            return_value=httpx.Response(200, json={"head": {"sha": head_sha or event.head_sha}})
        )
        self.reviews_list = respx.get(f"{self.base}/reviews").mock(
            return_value=httpx.Response(200, json=[])
        )
        self.reviews = respx.post(f"{self.base}/reviews").mock(
            return_value=httpx.Response(200, json={})
        )
        self.comments = respx.post(
            f"https://api.github.com/repos/{event.owner}/{event.repo}/issues/{event.number}/comments"
        ).mock(return_value=httpx.Response(201, json={}))

    def sent_review(self, index: int = 0) -> dict[str, Any]:
        return json.loads(self.reviews.calls[index].request.content)  # type: ignore[no-any-return]
