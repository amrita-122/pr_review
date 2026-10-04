import httpx

from app.github.auth import GITHUB_API


async def post_pr_comment(token: str, owner: str, repo: str, number: int, body: str) -> None:
    """PR conversation comments go through the issues API."""
    async with httpx.AsyncClient(base_url=GITHUB_API, timeout=10) as client:
        response = await client.post(
            f"/repos/{owner}/{repo}/issues/{number}/comments",
            json={"body": body},
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )
    response.raise_for_status()
