import hashlib
import hmac
import re
from datetime import datetime
from typing import Any

from pydantic import BaseModel

HANDLED_EVENT = "pull_request"
ISSUE_COMMENT_EVENT = "issue_comment"
HANDLED_ACTIONS = frozenset({"opened", "reopened", "synchronize", "ready_for_review"})
CLOSED_ACTION = "closed"  # merged or not: either way the PR is finished
CONFIRM_COMMAND = "/confirm"
DISMISS_COMMAND = "/dismiss"
REVIEW_COMMAND = "/review"  # re-run a review that failed, was skipped or was rate limited
REVIEW_COMMENT_EVENT = "pull_request_review_comment"
# A human GitHub login. It ends up in a URL path (/collaborators/{login}/permission).
LOGIN_PATTERN = re.compile(r"^[A-Za-z0-9-]{1,39}$")


class Installation(BaseModel):
    id: int


class Owner(BaseModel):
    login: str


class Repository(BaseModel):
    name: str
    owner: Owner
    # None = the payload did not say. Everything that depends on visibility treats None as PUBLIC:
    # the safe side for a tool that must not publish vulnerability details (ADR 0008).
    private: bool | None = None


class Head(BaseModel):
    sha: str


class Base(BaseModel):
    sha: str


class PrUser(BaseModel):
    login: str


class PullRequest(BaseModel):
    user: PrUser | None = None  # the PR author; only the login is read, and only if well-formed
    head: Head
    base: Base | None = None  # the commit the PR merges into: where trusted repo config is read
    # When GitHub last changed this PR. Used only to order out-of-order deliveries of pushes.
    updated_at: datetime | None = None
    draft: bool = False  # a draft is work in progress: reviewed only if REVIEW_DRAFTS is on


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

    @property
    def author_login(self) -> str | None:
        user = self.pull_request.user
        if user is None or LOGIN_PATTERN.fullmatch(user.login) is None:
            return None
        return user.login

    @property
    def base_sha(self) -> str | None:
        base = self.pull_request.base
        return base.sha if base is not None else None

    @property
    def repo_private(self) -> bool | None:
        return self.repository.private

    @property
    def is_draft(self) -> bool:
        return self.pull_request.draft

    @property
    def head_updated_at(self) -> datetime | None:
        return self.pull_request.updated_at


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
    user: CommentUser | None = None  # the author of the PR (a PR is an issue)
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
    def author_login(self) -> str | None:
        return self.issue.user.login if self.issue.user is not None else None

    @property
    def is_command_request(self) -> bool:
        """The ONE place that decides whether a PR comment may act as a command (/confirm or
        /dismiss). Which command it is: `command`."""
        return (
            self.action == "created"  # never "edited": otherwise edit an old comment into a command
            and self.issue.pull_request is not None  # plain-issue comments arrive as the same event
            and self.comment.user.type != "Bot"  # includes our own app
            and LOGIN_PATTERN.fullmatch(self.comment.user.login) is not None  # goes in a URL path
            and command_of(self.comment.body) is not None
        )

    @property
    def command(self) -> str | None:
        return command_of(self.comment.body)

    @property
    def is_confirm_request(self) -> bool:
        return self.is_command_request and self.command == "confirm"


class ReviewComment(BaseModel):
    id: int
    body: str
    user: CommentUser
    in_reply_to_id: int | None = None  # set when the comment is a reply in a review thread


class ReviewPullRequest(BaseModel):
    number: int
    user: CommentUser | None = None  # the PR's author


class PullRequestReviewCommentEvent(BaseModel):
    """A comment on the diff. Only a reply to one of OUR comments saying /dismiss matters."""

    action: str
    comment: ReviewComment
    pull_request: ReviewPullRequest
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
        return self.pull_request.number

    @property
    def login(self) -> str:
        return self.comment.user.login

    @property
    def author_login(self) -> str | None:
        return self.pull_request.user.login if self.pull_request.user is not None else None

    @property
    def is_dismiss_request(self) -> bool:
        return (
            self.action == "created"  # never "edited"
            and self.comment.in_reply_to_id is not None
            and self.comment.user.type != "Bot"
            and LOGIN_PATTERN.fullmatch(self.comment.user.login) is not None
            and command_of(self.comment.body) == "dismiss"
        )


def parse_review_comment_event(body: bytes) -> PullRequestReviewCommentEvent:
    return PullRequestReviewCommentEvent.model_validate_json(body)


def parse_issue_comment_event(body: bytes) -> IssueCommentEvent:
    """Parse a verified webhook body. Raises pydantic.ValidationError if malformed."""
    return IssueCommentEvent.model_validate_json(body)


def command_of(body: str) -> str | None:
    """ "confirm" or "dismiss" for an exact command, else None. Exact match only: our own
    comments mention the commands, so a substring test would let the bot command itself."""
    text = body.strip()
    if text == CONFIRM_COMMAND:
        return "confirm"
    if text == DISMISS_COMMAND:
        return "dismiss"
    if text == REVIEW_COMMAND:
        return "review"
    return None


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
