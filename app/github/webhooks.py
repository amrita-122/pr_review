import hashlib
import hmac
import re
from typing import Any

from pydantic import BaseModel

HANDLED_EVENT = "pull_request"
ISSUE_COMMENT_EVENT = "issue_comment"
HANDLED_ACTIONS = frozenset({"opened", "reopened", "synchronize"})
CONFIRM_COMMAND = "/confirm"
# A human GitHub login. It ends up in a URL path (/collaborators/{login}/permission).
LOGIN_PATTERN = re.compile(r"^[A-Za-z0-9-]{1,39}$")


class Installation(BaseModel):
    id: int


class Owner(BaseModel):
    login: str


class Repository(BaseModel):
    name: str
    owner: Owner


class Head(BaseModel):
    sha: str


class PullRequest(BaseModel):
    head: Head


class PullRequestEvent(BaseModel):
    """Only the fields we use; everything else in the payload is ignored."""

    action: str
    number: int
    installation: Installation
    repository: Repository
    pull_request: PullRequest

    @property
    def installation_id(self) -> int:
        return self.installation.id

    @property
    def owner(self) -> str:
        return self.repository.owner.login

    @property
    def repo(self) -> str:
        return self.repository.name

    @property
    def head_sha(self) -> str:
        return self.pull_request.head.sha


class CommentUser(BaseModel):
    # Not pattern-checked here: bot logins look like "app[bot]" and must still parse (they are
    # ignored later). LOGIN_PATTERN is applied in IssueCommentEvent.is_confirm_request.
    login: str
    type: str  # "User" or "Bot"


class Comment(BaseModel):
    id: int
    body: str
    user: CommentUser


class IssueRef(BaseModel):
    number: int
    # Only present when the comment is on a PR. Plain issues omit it.
    pull_request: dict[str, Any] | None = None


class IssueCommentEvent(BaseModel):
    """Only the fields we use. Everything else in the payload is ignored."""

    action: str
    issue: IssueRef
    comment: Comment
    repository: Repository
    installation: Installation

    @property
    def installation_id(self) -> int:
        return self.installation.id

    @property
    def owner(self) -> str:
        return self.repository.owner.login

    @property
    def repo(self) -> str:
        return self.repository.name

    @property
    def number(self) -> int:
        return self.issue.number

    @property
    def login(self) -> str:
        return self.comment.user.login

    @property
    def is_confirm_request(self) -> bool:
        """The ONE place that decides whether a comment may act as /confirm."""
        return (
            self.action == "created"  # never "edited": otherwise edit an old comment into /confirm
            and self.issue.pull_request is not None  # plain-issue comments arrive as the same event
            and self.comment.user.type != "Bot"  # includes our own app
            and LOGIN_PATTERN.fullmatch(self.comment.user.login) is not None  # goes in a URL path
            and is_confirm_command(self.comment.body)
        )


def parse_issue_comment_event(body: bytes) -> IssueCommentEvent:
    """Parse a verified webhook body. Raises pydantic.ValidationError if malformed."""
    return IssueCommentEvent.model_validate_json(body)


def is_confirm_command(body: str) -> bool:
    """Exact match only. The bot's own 'reply /confirm' comment contains the word,
    so `"/confirm" in body` would let the bot approve itself."""
    return body.strip() == CONFIRM_COMMAND


def parse_pull_request_event(body: bytes) -> PullRequestEvent:
    """Parse a verified webhook body. Raises pydantic.ValidationError if malformed."""
    return PullRequestEvent.model_validate_json(body)


def verify_signature(secret: str, body: bytes, header: str | None) -> bool:
    """
    Verify the signature of a GitHub webhook request.

    Args:
        secret (str): The webhook secret configured in GitHub.
        body (bytes): The raw request body.
        header (str | None): The value of the 'X-Hub-Signature-256' header from the request.

    Returns:
        bool: True if the signature is valid, False otherwise.
    """

    if header is None:
        return False

    # GitHub sends the signature in the format: sha256=signature
    try:
        sha_name, signature = header.split("=")
    except ValueError:
        return False

    if sha_name != "sha256":
        return False

    # Create a new HMAC object using the secret and the request body
    mac = hmac.new(secret.encode(), msg=body, digestmod=hashlib.sha256)

    # Compare the computed HMAC with the signature from the header
    return hmac.compare_digest(mac.hexdigest(), signature)
