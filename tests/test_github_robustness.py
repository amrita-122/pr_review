"""10.11: GitHub client robustness (Retry-After, exact truncation note, long review lists,
token invalidation on 401, webhook body cap)."""

from types import SimpleNamespace

import httpx
import pytest
import respx

from app.config import get_settings
from app.github import http
from app.github.client import list_pr_files, review_exists
from tests.test_webhook_signatures import client, post_signed


def _response(status: int, **headers: str) -> httpx.Response:
    return httpx.Response(status, headers=headers, request=httpx.Request("GET", "https://x"))


def _error(status: int, **headers: str) -> httpx.HTTPStatusError:
    response = _response(status, **headers)
    return httpx.HTTPStatusError("boom", request=response.request, response=response)


# --- Retry-After -------------------------------------------------------------------------------


def test_retry_after_is_read_only_when_it_is_a_plain_number_of_seconds() -> None:
    assert http.retry_after_seconds(_response(429, **{"retry-after": "7"})) == 7.0
    assert http.retry_after_seconds(_response(429)) is None
    assert http.retry_after_seconds(_response(429, **{"retry-after": "Wed, 21 Oct"})) is None
    assert http.retry_after_seconds(_response(429, **{"retry-after": "-5"})) is None


def test_secondary_rate_limit_403_is_retried_but_a_plain_403_is_not() -> None:
    assert http._retryable(_error(403, **{"retry-after": "3"})) is True
    assert http._retryable(_error(403)) is False
    assert http._retryable(_error(429)) is True and http._retryable(_error(404)) is False


def _state(exc: BaseException) -> SimpleNamespace:
    outcome = SimpleNamespace(exception=lambda: exc)
    return SimpleNamespace(attempt_number=1, outcome=outcome)


def test_wait_honours_retry_after_but_never_longer_than_the_cap() -> None:
    wait = http._wait(8.0)
    assert wait(_state(_error(429, **{"retry-after": "12"}))) == 12.0  # more than the backoff
    assert wait(_state(_error(429, **{"retry-after": "9999"}))) == http.RETRY_AFTER_CAP_SECONDS
    assert wait(_state(_error(500))) <= 8.0  # no header: plain backoff
    assert wait(_state(httpx.ConnectError("x"))) <= 8.0


# --- "(more files)" -----------------------------------------------------------------------------

FILES_URL = "https://api.github.com/repos/o/r/pulls/3/files"


def _page(n: int, *, next_page: bool) -> httpx.Response:
    headers = {"Link": f'<{FILES_URL}?page={n + 1}>; rel="next"'} if next_page else {}
    items = [{"filename": f"f{n}_{i}.py", "status": "modified", "patch": "@@"} for i in range(2)]
    return httpx.Response(200, json=items, headers=headers)


@respx.mock
async def test_exactly_as_many_files_as_the_cap_is_not_reported_as_truncated() -> None:
    respx.get(FILES_URL, params={"page": "2"}).mock(return_value=_page(2, next_page=True))
    respx.get(FILES_URL, params={"page": "3"}).mock(return_value=_page(3, next_page=False))
    respx.get(FILES_URL, params={"per_page": "100"}).mock(return_value=_page(1, next_page=True))
    listing = await list_pr_files("tok", "o", "r", 3, max_pages=3)
    assert len(listing) == 6 and listing.truncated is False


@respx.mock
async def test_more_pages_than_the_cap_is_reported_as_truncated() -> None:
    respx.get(FILES_URL, params={"page": "2"}).mock(return_value=_page(2, next_page=True))
    respx.get(FILES_URL, params={"per_page": "100"}).mock(return_value=_page(1, next_page=True))
    listing = await list_pr_files("tok", "o", "r", 3, max_pages=2)
    assert len(listing) == 4 and listing.truncated is True


# --- review_exists on a PR with many reviews --------------------------------------------------

REVIEWS_URL = "https://api.github.com/repos/o/r/pulls/3/reviews"
MARK = "<!-- m -->"


@respx.mock
async def test_review_exists_finds_ours_on_the_last_page_of_a_long_review_list() -> None:
    last = "https://api.github.com/repositories/1/pulls/3/reviews?per_page=100&page=9"
    prev = "https://api.github.com/repositories/1/pulls/3/reviews?per_page=100&page=8"
    first_links = f'<{prev.replace("page=8", "page=2")}>; rel="next", <{last}>; rel="last"'
    respx.get(REVIEWS_URL).mock(
        return_value=httpx.Response(200, json=[], headers={"Link": first_links})
    )
    respx.get(last).mock(
        return_value=httpx.Response(
            200,
            json=[{"commit_id": "abc", "body": f"hi {MARK}"}],
            headers={"Link": f'<{prev}>; rel="prev"'},
        )
    )
    # page 8 and page 2 are never needed: our review is on the newest page
    assert await review_exists("tok", "o", "r", 3, "abc", MARK) is True


@respx.mock
async def test_review_exists_walks_backwards_when_ours_is_not_on_the_very_last_page() -> None:
    last = "https://api.github.com/repositories/1/pulls/3/reviews?per_page=100&page=3"
    mid = "https://api.github.com/repositories/1/pulls/3/reviews?per_page=100&page=2"
    respx.get(REVIEWS_URL).mock(
        return_value=httpx.Response(
            200, json=[], headers={"Link": f'<{mid}>; rel="next", <{last}>; rel="last"'}
        )
    )
    respx.get(last).mock(
        return_value=httpx.Response(200, json=[], headers={"Link": f'<{mid}>; rel="prev"'})
    )
    respx.get(mid).mock(return_value=httpx.Response(200, json=[{"commit_id": "abc", "body": MARK}]))
    assert await review_exists("tok", "o", "r", 3, "abc", MARK) is True


# --- 401 -> the cached installation token is dropped ---------------------------------------


@respx.mock
async def test_a_401_in_the_confirm_job_invalidates_the_cached_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.jobs import handlers
    from tests.helpers import Pipeline
    from tests.test_confirm_job import EVENT, PERMISSION_URL, confirm

    Pipeline(EVENT)
    respx.get(PERMISSION_URL).mock(return_value=httpx.Response(401))
    dropped: list[int] = []
    monkeypatch.setattr(handlers, "invalidate_installation_token", dropped.append)
    with pytest.raises(httpx.HTTPStatusError):
        await handlers.run_confirm_job(confirm())
    assert dropped == [EVENT.installation_id]


@respx.mock
async def test_a_401_while_posting_the_pending_comment_invalidates_the_token(
    monkeypatch: pytest.MonkeyPatch, db_session: object
) -> None:
    from app.db import runs
    from app.jobs import handlers
    from tests.helpers import Pipeline
    from tests.test_confirm_job import EVENT, REPO, RUN_ID

    gh = Pipeline(EVENT)
    gh.comments.mock(return_value=httpx.Response(401))
    runs.create_queued(
        db_session, run_id=RUN_ID, repo=REPO, pr_number=EVENT.number,  # type: ignore[arg-type]
        head_sha=EVENT.head_sha, prompt_version="v", model="m",
    )  # fmt: skip
    runs.mark_running(db_session, RUN_ID, prompt_version="v", model="m")  # type: ignore[arg-type]
    runs.mark_awaiting(db_session, RUN_ID, installation_id=EVENT.installation_id, held=1)  # type: ignore[arg-type]
    dropped: list[int] = []
    monkeypatch.setattr(handlers, "invalidate_installation_token", dropped.append)
    monkeypatch.setenv("CONFIRMATION_ENABLED", "true")
    get_settings.cache_clear()

    assert await handlers.announce_unannounced_holds(get_settings()) == 0
    assert dropped == [EVENT.installation_id]


# --- webhook body cap -------------------------------------------------------------------------


def test_an_oversized_webhook_body_is_refused_before_the_signature_is_checked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MAX_WEBHOOK_BODY_BYTES", "100")
    get_settings.cache_clear()
    big = b"x" * 101
    # even a CORRECT signature does not matter: nothing is hashed or parsed
    assert post_signed(big).status_code == 413
    # and a chunked body without a declared length is cut off while streaming
    resp = client.post(
        "/webhooks/github",
        content=iter([b"x" * 60, b"x" * 60]),
        headers={"X-GitHub-Event": "pull_request", "X-Hub-Signature-256": "sha256=bad"},
    )
    assert resp.status_code == 413


def test_a_normal_sized_body_is_still_verified_normally() -> None:
    assert (
        client.post(
            "/webhooks/github",
            content=b"{}",
            headers={"X-GitHub-Event": "pull_request", "X-Hub-Signature-256": "sha256=bad"},
        ).status_code
        == 401
    )
