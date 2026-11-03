import httpx
from pydantic import BaseModel

from app.github.http import get_with_retry, github_client

FILES_PER_PAGE = 100
REVIEW_TIMEOUT = httpx.Timeout(20.0, connect=5.0)  # posting a big review takes longer than a read
MAX_REVIEW_PAGES = 5
# GitHub's pulls/{n}/files endpoint returns at most 3000 files (30 pages of 100).
MAX_PAGES = 30


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
) -> list[ChangedFile]:
    """All changed files of a PR, following the Link header across pages.

    Files without a patch are returned too; check `skip_reason`. `max_pages` bounds how much a
    huge PR can make us fetch (it is attacker-controlled size).
    """
    files: list[ChangedFile] = []
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
    return files


class ReviewComment(BaseModel):
    path: str
    line: int
    body: str
    side: str = "RIGHT"  # comment on the new version of the file


async def post_review(
    token: str,
    owner: str,
    repo: str,
    number: int,
    commit_id: str,
    body: str,
    comments: list[ReviewComment],
) -> None:
    """Post ONE review. The event is always COMMENT: the bot never approves or requests changes."""
    payload: dict[str, object] = {
        "commit_id": commit_id,
        "event": "COMMENT",
        "body": body,
        "comments": [c.model_dump() for c in comments],
    }
    async with github_client(REVIEW_TIMEOUT) as client:
        response = await client.post(
            f"/repos/{owner}/{repo}/pulls/{number}/reviews",
            json=payload,
            headers=_headers(token),
        )
    response.raise_for_status()


async def get_pr_head_sha(token: str, owner: str, repo: str, number: int) -> str:
    """The PR's CURRENT head commit. Used right before posting to detect a newer push."""
    async with github_client() as client:
        response = await get_with_retry(
            client, f"/repos/{owner}/{repo}/pulls/{number}", headers=_headers(token)
        )
    return str(response.json()["head"]["sha"])


async def review_exists(
    token: str, owner: str, repo: str, number: int, commit_id: str, marker: str
) -> bool:
    """True if a review on this commit already carries our marker (an earlier attempt posted it)."""
    async with github_client() as client:
        url = f"/repos/{owner}/{repo}/pulls/{number}/reviews"
        params: dict[str, int] | None = {"per_page": FILES_PER_PAGE}
        for _ in range(MAX_REVIEW_PAGES):
            response = await get_with_retry(client, url, params=params, headers=_headers(token))
            for review in response.json():
                body = review.get("body") or ""
                if review.get("commit_id") == commit_id and marker in body:
                    return True
            next_url = response.links.get("next", {}).get("url")
            if next_url is None:
                return False
            url, params = next_url, None
    return False
