from typing import Literal

from pydantic import BaseModel, Field

from app.github.webhooks import PullRequestEvent

REVIEW_PR = "review_pr"
CONFIRM_REVIEW = "confirm_review"


class ReviewJobPayload(BaseModel):
    """The only data a review job needs. Re-validated when the worker reads it back."""

    run_id: str
    installation_id: int
    owner: str
    repo: str
    number: int
    head_sha: str

    @property
    def repo_full_name(self) -> str:
        return f"{self.owner}/{self.repo}"

    @classmethod
    def from_event(cls, event: PullRequestEvent, run_id: str) -> "ReviewJobPayload":
        return cls(
            run_id=run_id,
            installation_id=event.installation_id,
            owner=event.owner,
            repo=event.repo,
            number=event.number,
            head_sha=event.head_sha,
        )


class ConfirmJobPayload(BaseModel):
    """A maintainer's /confirm. The permission check happens later, in the worker."""

    run_id: str
    installation_id: int
    owner: str
    repo: str
    number: int
    comment_id: int
    commenter_login: str = Field(pattern=r"^[A-Za-z0-9-]{1,39}$")  # goes into a URL path
    author_login: str | None = None  # the PR's author: may not confirm their own review
    command: Literal["confirm", "dismiss"] = "confirm"
    # /dismiss as a reply in a finding's thread: the id of the finding's comment (None = the
    # held review as a whole).
    target_comment_id: int | None = None

    @property
    def repo_full_name(self) -> str:
        return f"{self.owner}/{self.repo}"
