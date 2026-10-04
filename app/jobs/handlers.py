import logging

from app.github.auth import get_installation_token
from app.github.client import post_pr_comment
from app.github.webhooks import PullRequestEvent

logger = logging.getLogger(__name__)


async def process_pull_request(event: PullRequestEvent) -> None:
    """Background job. Must never raise: nobody is waiting on it."""
    try:
        token = await get_installation_token(event.installation_id)
        await post_pr_comment(
            token, event.owner, event.repo, event.number, "Review bot received this PR"
        )
    except Exception:
        logger.exception("failed to process PR %s/%s#%s", event.owner, event.repo, event.number)
