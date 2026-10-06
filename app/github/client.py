import httpx
from pydantic import BaseModel

from app.github.auth import GITHUB_API

FILES_PER_PAGE = 100
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
    async with httpx.AsyncClient(base_url=GITHUB_API, timeout=10) as client:
        response = await client.post(
            f"/repos/{owner}/{repo}/issues/{number}/comments",
            json={"body": body},
            headers=_headers(token),
        )
    response.raise_for_status()


async def list_pr_files(token: str, owner: str, repo: str, number: int) -> list[ChangedFile]:
    """All changed files of a PR, following the Link header across pages.

    Files without a patch are returned too; check `skip_reason`.
    """
    files: list[ChangedFile] = []
    async with httpx.AsyncClient(base_url=GITHUB_API, timeout=10) as client:
        # The first request carries params; "next" URLs are absolute and already have them.
        url = f"/repos/{owner}/{repo}/pulls/{number}/files"
        params: dict[str, int] | None = {"per_page": FILES_PER_PAGE}
        for _ in range(MAX_PAGES):
            response = await client.get(url, params=params, headers=_headers(token))
            response.raise_for_status()
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
    async with httpx.AsyncClient(base_url=GITHUB_API, timeout=20) as client:
        response = await client.post(
            f"/repos/{owner}/{repo}/pulls/{number}/reviews",
            json=payload,
            headers=_headers(token),
        )
    response.raise_for_status()
