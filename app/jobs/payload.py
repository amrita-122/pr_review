from pydantic import BaseModel

from app.github.webhooks import PullRequestEvent

REVIEW_PR = "review_pr"


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
