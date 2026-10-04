import hashlib
import hmac

from pydantic import BaseModel

HANDLED_EVENT = "pull_request"
HANDLED_ACTIONS = frozenset({"opened", "reopened", "synchronize"})


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
