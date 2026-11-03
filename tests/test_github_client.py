import json

import httpx
import jwt
import respx

from app.github import auth
from app.github.client import ReviewComment, list_pr_files, post_pr_comment, post_review
from app.github.webhooks import PullRequestEvent
from tests.helpers import PRIVATE_PEM, PUBLIC_PEM, token_response


def test_app_jwt_claims() -> None:
    token = auth.build_app_jwt("12345", PRIVATE_PEM, now=1_000_000)
    claims = jwt.decode(token, PUBLIC_PEM, algorithms=["RS256"], options={"verify_exp": False})
    assert claims == {"iat": 1_000_000 - 60, "exp": 1_000_000 + 540, "iss": "12345"}


@respx.mock
async def test_token_exchange_and_cache() -> None:
    route = respx.post("https://api.github.com/app/installations/7/access_tokens").mock(
        return_value=token_response()
    )
    assert await auth.get_installation_token(7) == "ghs_tok"
    assert await auth.get_installation_token(7) == "ghs_tok"
    assert route.call_count == 1
    assert route.calls[0].request.headers["Authorization"].startswith("Bearer ")


@respx.mock
async def test_token_refreshed_when_near_expiry() -> None:
    route = respx.post("https://api.github.com/app/installations/7/access_tokens").mock(
        return_value=token_response(minutes=2)
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


@respx.mock
async def test_list_pr_files_stops_at_max_pages() -> None:
    page = "https://api.github.com/repositories/1/pulls/3/files?per_page=100&page={}"
    respx.get("https://api.github.com/repos/o/r/pulls/3/files").mock(
        return_value=httpx.Response(
            200, json=[_file("a.py")], headers={"Link": f'<{page.format(2)}>; rel="next"'}
        )
    )
    second = respx.get(page.format(2)).mock(
        return_value=httpx.Response(
            200, json=[_file("b.py")], headers={"Link": f'<{page.format(3)}>; rel="next"'}
        )
    )
    third = respx.get(page.format(3)).mock(return_value=httpx.Response(200, json=[_file("c.py")]))
    files = await list_pr_files("tok", "o", "r", 3, max_pages=2)
    assert [f.filename for f in files] == ["a.py", "b.py"]
    assert second.call_count == 1 and third.call_count == 0


def test_pull_request_event_does_not_parse_pr_text() -> None:
    # Adding title/body here would be a new attack channel: it needs an ADR and a red-team case.
    from app.github.webhooks import PullRequest

    assert not {"title", "body"} & set(PullRequestEvent.model_fields)
    assert set(PullRequest.model_fields) == {"head"}


def test_github_client_cannot_approve_merge_or_request_changes() -> None:
    from app.github import client

    public = {name.lower() for name in dir(client) if not name.startswith("_")}
    assert not any(word in name for name in public for word in ("approve", "merge", "dismiss"))
    assert {"post_review", "post_pr_comment", "list_pr_files"} <= public
