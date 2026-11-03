"""Turn any exception into a (reason, transient?) decision. Pure: no IO.

The message is built from the exception TYPE and an HTTP status only. Exception text can carry
request URLs, repo names or model output, and it ends up in the database and logs.
"""

import anthropic
import httpx

from app.review.llm import ReviewFailed

# Why a job failed, stored on the run: where the problem was.
TIMEOUT = "timeout"
GITHUB = "github"
MODEL = "model"
INTERNAL = "internal"


class JobFailure(Exception):  # noqa: N818
    """A classified failure. transient=True means retrying later may work."""

    def __init__(self, reason: str, transient: bool, message: str) -> None:
        super().__init__(message)
        self.reason = reason
        self.transient = transient
        self.message = message


def _github_status(exc: httpx.HTTPStatusError) -> JobFailure:
    status = exc.response.status_code
    rate_limited = exc.response.headers.get("x-ratelimit-remaining") == "0" or (
        "retry-after" in exc.response.headers
    )
    # 401: our cached installation token may have expired early; a fresh one can fix it.
    transient = status in (401, 429) or status >= 500 or (status == 403 and rate_limited)
    return JobFailure(GITHUB, transient, f"GitHub HTTP {status}")


def classify(exc: BaseException) -> JobFailure:
    if isinstance(exc, JobFailure):
        return exc
    if isinstance(exc, TimeoutError):
        return JobFailure(TIMEOUT, True, "TimeoutError")
    if isinstance(exc, httpx.HTTPStatusError):
        return _github_status(exc)
    if isinstance(exc, httpx.TransportError):
        return JobFailure(GITHUB, True, f"GitHub {type(exc).__name__}")
    if isinstance(
        exc,
        anthropic.RateLimitError | anthropic.InternalServerError | anthropic.APIConnectionError,
    ):
        return JobFailure(MODEL, True, f"model {type(exc).__name__}")
    if isinstance(exc, anthropic.APIStatusError):
        # 400 (bad request, no credits), 401, 403: retrying cannot help.
        return JobFailure(MODEL, False, f"model HTTP {exc.status_code}")
    if isinstance(exc, ReviewFailed):
        return JobFailure(MODEL, True, "ReviewFailed")
    return JobFailure(INTERNAL, True, type(exc).__name__)
