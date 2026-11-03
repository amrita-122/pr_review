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


def _retryable(exc: BaseException) -> bool:
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code == 429 or exc.response.status_code >= 500
    return isinstance(exc, httpx.TransportError)  # includes timeouts


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
        wait=wait_exponential(multiplier=0.5, max=wait_max),
        stop=stop_after_attempt(attempts),
        reraise=True,
    ):
        with attempt:
            response = await client.get(url, params=params, headers=headers)
            response.raise_for_status()
            return response
    raise AssertionError("unreachable")  # pragma: no cover
