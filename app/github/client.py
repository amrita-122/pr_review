import re
from typing import Any

import httpx
import structlog
from pydantic import BaseModel

from app.github.http import get_with_retry, github_client

logger = structlog.get_logger()

FILES_PER_PAGE = 100
REVIEW_TIMEOUT = httpx.Timeout(20.0, connect=5.0)  # posting a big review takes longer than a read
MAX_REVIEW_PAGES = 5
# GitHub's pulls/{n}/files endpoint returns at most 3000 files (30 pages of 100).
MAX_PAGES = 30


class FileListing(list["ChangedFile"]):
    """The listed files, plus whether GitHub had MORE that we did not fetch (page cap)."""

    truncated: bool = False


class ChangedFile(BaseModel):
    """One file of a PR. Only the fields we use; everything else GitHub sends is ignored."""

    filename: str
    status: str
    patch: str | None = None

    @property
    def skip_reason(self) -> str | None:
        """Why this file can't be reviewed, or None if it has a patch."""
        if self.patch is None:
            return "no patch (binary or too large)"
        return None


def _headers(token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


async def post_pr_comment(token: str, owner: str, repo: str, number: int, body: str) -> None:
    """PR conversation comments go through the issues API."""
    async with github_client() as client:
        response = await client.post(
            f"/repos/{owner}/{repo}/issues/{number}/comments",
            json={"body": body},
            headers=_headers(token),
        )
    response.raise_for_status()


async def list_pr_files(
    token: str, owner: str, repo: str, number: int, max_pages: int = MAX_PAGES
) -> FileListing:
    """All changed files of a PR, following the Link header across pages.

    Files without a patch are returned too; check `skip_reason`. `max_pages` bounds how much a
    huge PR can make us fetch (it is attacker-controlled size).
    """
    files = FileListing()
    async with github_client() as client:
        # The first request carries params; "next" URLs are absolute and already have them.
        url = f"/repos/{owner}/{repo}/pulls/{number}/files"
        params: dict[str, int] | None = {"per_page": FILES_PER_PAGE}
        for _ in range(min(max_pages, MAX_PAGES)):
            response = await get_with_retry(client, url, params=params, headers=_headers(token))
            files.extend(ChangedFile.model_validate(item) for item in response.json())
            next_url = response.links.get("next", {}).get("url")
            if next_url is None:
                break
            url = next_url
            params = None
        else:
            files.truncated = True  # the page cap ended the loop while GitHub had a next page
    return files


class ReviewComment(BaseModel):
    path: str
    line: int
    body: str
    side: str = "RIGHT"  # RIGHT = new version of the file, LEFT = a deleted line (old numbers)
    start_line: int | None = None  # set for a multi-line comment: it covers start_line..line
    start_side: str | None = None


class PostedComment(BaseModel):
    """A review comment as GitHub stored it."""

    id: int
    node_id: str | None = None
    path: str
    line: int | None = None
    original_line: int | None = None

    @property
    def placed_line(self) -> int | None:
        return self.line if self.line is not None else self.original_line


class PostedReview(BaseModel):
    """What GitHub says about a review we just posted. Every field may be missing."""

    id: int | None = None
    node_id: str | None = None
    comments: list[PostedComment] = []


async def post_review(
    token: str,
    owner: str,
    repo: str,
    number: int,
    commit_id: str,
    body: str,
    comments: list[ReviewComment],
) -> PostedReview:
    """Post ONE review. The event is always COMMENT: the bot never approves or requests changes.

    Returns GitHub's ids for the review and its comments, so findings can be found again later.
    Looking the comments up is best effort: the review is already posted, so a failure there
    returns the review without comment ids instead of raising.
    """
    payload: dict[str, object] = {
        "commit_id": commit_id,
        "event": "COMMENT",
        "body": body,
        "comments": [c.model_dump(exclude_none=True) for c in comments],
    }
    async with github_client(REVIEW_TIMEOUT) as client:
        response = await client.post(
            f"/repos/{owner}/{repo}/pulls/{number}/reviews",
            json=payload,
            headers=_headers(token),
        )
    response.raise_for_status()
    try:
        data = response.json()
    except ValueError:
        return PostedReview()
    if not isinstance(data, dict) or not isinstance(data.get("id"), int):
        return PostedReview()
    posted = PostedReview(id=data["id"], node_id=data.get("node_id"))
    if comments:
        try:
            posted.comments = await _list_review_comments(token, owner, repo, number, posted.id)
        except Exception as exc:  # the review is already on GitHub: never fail the job for this
            logger.warning("review_comment_ids_not_fetched", error=type(exc).__name__)
    return posted


async def _list_review_comments(
    token: str, owner: str, repo: str, number: int, review_id: int | None
) -> list[PostedComment]:
    async with github_client() as client:
        response = await get_with_retry(
            client,
            f"/repos/{owner}/{repo}/pulls/{number}/reviews/{review_id}/comments",
            params={"per_page": FILES_PER_PAGE},
            headers=_headers(token),
        )
    return [PostedComment.model_validate(item) for item in response.json()]


async def reply_to_review_comment(
    token: str, owner: str, repo: str, number: int, comment_id: int, body: str
) -> None:
    """Answer one of our own inline comments (a reply in its thread). Never edits or deletes."""
    async with github_client() as client:
        response = await client.post(
            f"/repos/{owner}/{repo}/pulls/{number}/comments/{comment_id}/replies",
            json={"body": body},
            headers=_headers(token),
        )
    response.raise_for_status()


_THREADS_QUERY = """
query($owner: String!, $repo: String!, $number: Int!, $after: String) {
  repository(owner: $owner, name: $repo) {
    pullRequest(number: $number) {
      reviewThreads(first: 100, after: $after) {
        pageInfo { hasNextPage endCursor }
        nodes { id isResolved comments(first: 1) { nodes { databaseId } } }
      }
    }
  }
}
"""
_RESOLVE_MUTATION = """
mutation($threadId: ID!) {
  resolveReviewThread(input: {threadId: $threadId}) { thread { isResolved } }
}
"""
MAX_THREAD_PAGES = 5


async def _graphql(
    client: httpx.AsyncClient, token: str, query: str, **variables: object
) -> dict[str, Any]:
    response = await client.post(
        "/graphql", json={"query": query, "variables": variables}, headers=_headers(token)
    )
    response.raise_for_status()
    data = response.json()
    if data.get("errors"):
        # GraphQL reports failures with HTTP 200. The messages can echo input: type only.
        raise RuntimeError("GitHub GraphQL returned errors")
    return data["data"]  # type: ignore[no-any-return]


async def resolve_review_thread(
    token: str, owner: str, repo: str, number: int, comment_id: int
) -> bool:
    """Mark the review thread that starts with this comment as resolved (the comment stays).

    Returns False when no such thread is found. A thread that is already resolved is fine.
    """
    async with github_client(REVIEW_TIMEOUT) as client:
        after: str | None = None
        for _ in range(MAX_THREAD_PAGES):
            data = await _graphql(
                client, token, _THREADS_QUERY, owner=owner, repo=repo, number=number, after=after
            )
            threads = data["repository"]["pullRequest"]["reviewThreads"]
            for node in threads["nodes"]:
                first = node["comments"]["nodes"]
                if first and first[0]["databaseId"] == comment_id:
                    if not node["isResolved"]:
                        await _graphql(client, token, _RESOLVE_MUTATION, threadId=node["id"])
                    return True
            if not threads["pageInfo"]["hasNextPage"]:
                return False
            after = threads["pageInfo"]["endCursor"]
    return False


async def get_pr_head_sha(token: str, owner: str, repo: str, number: int) -> str:
    """The PR's CURRENT head commit. Used right before posting to detect a newer push."""
    async with github_client() as client:
        response = await get_with_retry(
            client, f"/repos/{owner}/{repo}/pulls/{number}", headers=_headers(token)
        )
    return str(response.json()["head"]["sha"])


async def get_pr_refs(token: str, owner: str, repo: str, number: int) -> tuple[str, str | None]:
    """(head sha, base sha) of the PR right now."""
    async with github_client() as client:
        response = await get_with_retry(
            client, f"/repos/{owner}/{repo}/pulls/{number}", headers=_headers(token)
        )
    data = response.json()
    base = data.get("base")
    base_sha = base.get("sha") if isinstance(base, dict) else None
    return str(data["head"]["sha"]), base_sha if isinstance(base_sha, str) else None


async def review_exists(
    token: str, owner: str, repo: str, number: int, commit_id: str, marker: str
) -> bool:
    """True if a review on this commit already carries our marker (an earlier attempt posted it).

    GitHub lists reviews oldest first, so on a PR with many reviews ours is near the END: jump to
    the last page and walk backwards (at most MAX_REVIEW_PAGES pages). Without a `last` link
    (a short list) walk forward as usual.
    """
    async with github_client() as client:
        url = f"/repos/{owner}/{repo}/pulls/{number}/reviews"
        params: dict[str, int] | None = {"per_page": FILES_PER_PAGE}
        response = await get_with_retry(client, url, params=params, headers=_headers(token))
        direction = "next"
        last_url = response.links.get("last", {}).get("url")
        if last_url is not None and response.links.get("next") is not None:
            response = await get_with_retry(client, last_url, headers=_headers(token))
            direction = "prev"
        for _ in range(MAX_REVIEW_PAGES):
            for review in response.json():
                body = review.get("body") or ""
                if review.get("commit_id") == commit_id and marker in body:
                    return True
            step_url = response.links.get(direction, {}).get("url")
            if step_url is None:
                return False
            response = await get_with_retry(client, step_url, headers=_headers(token))
    return False


LOGIN_PATTERN = re.compile(r"^[A-Za-z0-9-]{1,39}$")


async def get_collaborator_permission(token: str, owner: str, repo: str, login: str) -> str | None:
    """The user's permission on the repo: admin | write | read | none, or None if GitHub says 404.

    The login comes from a webhook payload, i.e. from outside: it is checked before it goes into
    the URL path. Callers must fail CLOSED: anything but a definite admin/write means no.
    """
    if not LOGIN_PATTERN.fullmatch(login):
        raise ValueError("invalid GitHub login")
    async with github_client() as client:
        try:
            response = await get_with_retry(
                client,
                f"/repos/{owner}/{repo}/collaborators/{login}/permission",
                headers=_headers(token),
            )
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                return None
            raise
    permission = response.json().get("permission")
    return permission if isinstance(permission, str) else None
