"""One place for how we talk to GitHub: timeouts on every call, retries on idempotent reads."""

from typing import Any

import httpx
from tenacity import (
    AsyncRetrying,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential,
)

GITHUB_API = "https://api.github.com"
# connect 5s, everything else 10s. A hung GitHub must never hang a job slot.
TIMEOUT = httpx.Timeout(10.0, connect=5.0)
READ_ATTEMPTS = 3


def github_client(timeout: httpx.Timeout = TIMEOUT) -> httpx.AsyncClient:
    return httpx.AsyncClient(base_url=GITHUB_API, timeout=timeout)


RETRY_AFTER_CAP_SECONDS = 30.0  # never let a header park a job for long; the job retries later


def retry_after_seconds(response: httpx.Response) -> float | None:
    """The server's Retry-After (whole seconds), or None if absent or not a plain number."""
    raw = response.headers.get("retry-after")
    if raw is None or not raw.strip().isdigit():
        return None
    return float(raw.strip())


def _retryable(exc: BaseException) -> bool:
    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
        # 403 + Retry-After is the secondary rate limit.
        return (
            status == 429
            or status >= 500
            or (status == 403 and retry_after_seconds(exc.response) is not None)
        )
    return isinstance(exc, httpx.TransportError)  # includes timeouts


def _wait(wait_max: float) -> Any:
    backoff = wait_exponential(multiplier=0.5, max=wait_max)

    def wait(retry_state: Any) -> float:
        delay: float = backoff(retry_state)
        exc = retry_state.outcome.exception() if retry_state.outcome else None
        if isinstance(exc, httpx.HTTPStatusError):
            asked = retry_after_seconds(exc.response)
            if asked is not None:  # honour it, within the cap
                return min(max(asked, delay), RETRY_AFTER_CAP_SECONDS)
        return delay

    return wait


async def get_with_retry(
    client: httpx.AsyncClient,
    url: str,
    *,
    params: dict[str, Any] | None = None,
    headers: dict[str, str],
    attempts: int = READ_ATTEMPTS,
    wait_max: float = 8.0,
) -> httpx.Response:
    """GET with retries on 5xx / 429 / network errors. Reads only: POSTs are never retried here."""
    async for attempt in AsyncRetrying(
        retry=retry_if_exception(_retryable),
        wait=_wait(wait_max),
        stop=stop_after_attempt(attempts),
        reraise=True,
    ):
        with attempt:
            response = await client.get(url, params=params, headers=headers)
            response.raise_for_status()
            return response
    raise AssertionError("unreachable")  # pragma: no cover
