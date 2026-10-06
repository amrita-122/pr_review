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
from app.github.client import ReviewComment, list_pr_files, post_pr_comment, post_review
from app.github.webhooks import PullRequestEvent, parse_pull_request_event
from app.jobs.handlers import process_pull_request
from app.review.schemas import Finding
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


FILES = [
    {
        "filename": "app/db.py",
        "status": "modified",
        "patch": "@@ -1,2 +1,3 @@\n import os\n+password = 'hunter2'\n print(1)",
    },
    {"filename": "uv.lock", "status": "modified", "patch": "@@ -1 +1 @@\n-a\n+b"},
    {"filename": "logo.png", "status": "added"},
]


def _mock_pipeline(event: PullRequestEvent) -> respx.Route:
    base = f"https://api.github.com/repos/{event.owner}/{event.repo}/pulls/{event.number}"
    respx.post(
        f"https://api.github.com/app/installations/{event.installation_id}/access_tokens"
    ).mock(return_value=_token_response())
    respx.get(f"{base}/files").mock(return_value=httpx.Response(200, json=FILES))
    return respx.post(f"{base}/reviews")


@respx.mock
async def test_post_review_is_always_comment_event() -> None:
    route = respx.post("https://api.github.com/repos/o/r/pulls/3/reviews").mock(
        return_value=httpx.Response(200, json={})
    )
    await post_review(
        "tok", "o", "r", 3, "abc", "hi", [ReviewComment(path="a.py", line=2, body="b")]
    )
    sent = json.loads(route.calls[0].request.content)
    assert sent == {
        "commit_id": "abc",
        "event": "COMMENT",
        "body": "hi",
        "comments": [{"path": "a.py", "line": 2, "body": "b", "side": "RIGHT"}],
    }


@respx.mock
async def test_process_pull_request_posts_one_review() -> None:
    event = parse_pull_request_event(FIXTURE_PATH.read_bytes())
    reviews = _mock_pipeline(event).mock(return_value=httpx.Response(200, json={}))
    await process_pull_request(event)
    assert reviews.call_count == 1
    sent = json.loads(reviews.calls[0].request.content)
    assert sent["commit_id"] == event.head_sha
    assert sent["event"] == "COMMENT"
    assert [(c["path"], c["line"]) for c in sent["comments"]] == [("app/db.py", 2)]
    assert "uv.lock" in sent["body"] and "logo.png" in sent["body"]


@respx.mock
async def test_wrong_line_is_dropped_and_rest_of_review_still_posts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.jobs import handlers
    from app.review.fake import review_file

    def with_bad_line(path: str, numbered: str) -> list[Finding]:
        real = review_file(path, numbered)
        return [*real, real[0].model_copy(update={"line": 999})]

    monkeypatch.setattr(handlers, "review_file", with_bad_line)
    event = parse_pull_request_event(FIXTURE_PATH.read_bytes())
    reviews = _mock_pipeline(event).mock(return_value=httpx.Response(200, json={}))
    await process_pull_request(event)
    sent = json.loads(reviews.calls[0].request.content)
    assert [c["line"] for c in sent["comments"]] == [2]


@respx.mock
async def test_falls_back_to_summary_when_github_rejects_comments() -> None:
    event = parse_pull_request_event(FIXTURE_PATH.read_bytes())
    reviews = _mock_pipeline(event).mock(
        side_effect=[httpx.Response(422, json={}), httpx.Response(200, json={})]
    )
    await process_pull_request(event)
    assert reviews.call_count == 2
    assert json.loads(reviews.calls[1].request.content)["comments"] == []


@respx.mock
async def test_process_pull_request_swallows_errors() -> None:
    event = parse_pull_request_event(FIXTURE_PATH.read_bytes())
    respx.post(
        f"https://api.github.com/app/installations/{event.installation_id}/access_tokens"
    ).mock(return_value=httpx.Response(500))
    await process_pull_request(event)  # must not raise


def _file(name: str, patch: str | None = "@@ -1 +1 @@\n-a\n+b") -> dict[str, object]:
    item: dict[str, object] = {"filename": name, "status": "modified", "sha": "x"}
    if patch is not None:
        item["patch"] = patch
    return item


@respx.mock
async def test_list_pr_files_single_page() -> None:
    route = respx.get("https://api.github.com/repos/o/r/pulls/3/files").mock(
        return_value=httpx.Response(200, json=[_file("a.py")])
    )
    files = await list_pr_files("tok", "o", "r", 3)
    assert [f.filename for f in files] == ["a.py"]
    assert route.calls[0].request.url.params["per_page"] == "100"
    assert route.calls[0].request.headers["Authorization"] == "Bearer tok"


@respx.mock
async def test_list_pr_files_follows_link_header() -> None:
    page2 = "https://api.github.com/repositories/1/pulls/3/files?per_page=100&page=2"
    respx.get("https://api.github.com/repos/o/r/pulls/3/files").mock(
        return_value=httpx.Response(
            200, json=[_file("a.py")], headers={"Link": f'<{page2}>; rel="next"'}
        )
    )
    second = respx.get(page2).mock(return_value=httpx.Response(200, json=[_file("b.py")]))
    files = await list_pr_files("tok", "o", "r", 3)
    assert [f.filename for f in files] == ["a.py", "b.py"]
    assert second.call_count == 1


@respx.mock
async def test_list_pr_files_marks_missing_patch_as_skipped() -> None:
    respx.get("https://api.github.com/repos/o/r/pulls/3/files").mock(
        return_value=httpx.Response(200, json=[_file("img.png", patch=None), _file("a.py")])
    )
    files = await list_pr_files("tok", "o", "r", 3)
    assert files[0].skip_reason is not None
    assert files[1].skip_reason is None
