"""The LLM reviewer: one file's numbered diff -> list[Finding]. Only module that calls a model."""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from pathlib import Path

import anthropic
from langchain_anthropic import ChatAnthropic
from langchain_core.exceptions import OutputParserException
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import BaseMessage, HumanMessage, SystemMessage
from pydantic import ValidationError
from tenacity import (
    AsyncRetrying,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)
from tenacity.wait import wait_base

from app.config import Settings
from app.review.schemas import Finding, ReviewResult

logger = logging.getLogger(__name__)

PROMPT_VERSION = "security_v1"
_PROMPT_DIR = Path(__file__).parent / "prompts"

# Same inputs as the fake reviewer, but async because it waits on the network.
Reviewer = Callable[[str, str], Awaitable[list[Finding]]]

# Worth retrying with backoff: rate limits, 5xx, network errors/timeouts (APITimeoutError is an
# APIConnectionError) and our own asyncio timeout.
_TRANSIENT = (
    anthropic.RateLimitError,
    anthropic.InternalServerError,
    anthropic.APIConnectionError,
    TimeoutError,
)
# The model answered, but not with a valid ReviewResult.
_BAD_OUTPUT = (ValidationError, OutputParserException)


class ReviewFailed(Exception):  # noqa: N818
    """The model could not produce a usable review for this file."""


def load_prompt(version: str = PROMPT_VERSION) -> str:
    return (_PROMPT_DIR / f"{version}.md").read_text(encoding="utf-8")


def build_chat_model(settings: Settings) -> BaseChatModel:
    # max_retries=0: tenacity owns retries so there is exactly one place that decides them.
    return ChatAnthropic(  # type: ignore[call-arg]
        model=settings.model_review,
        api_key=settings.anthropic_api_key,
        timeout=settings.model_timeout_seconds,
        max_retries=0,
    )


def _user_message(path: str, numbered_diff: str) -> str:
    # The diff is attacker-controlled: stop it from closing the marker early and "escaping" it.
    safe = numbered_diff.replace("<<<END_UNTRUSTED_DIFF>>>", "")
    return f"File path: {path}\n\n<<<UNTRUSTED_DIFF>>>\n{safe}\n<<<END_UNTRUSTED_DIFF>>>"


def make_reviewer(
    model: BaseChatModel,
    *,
    prompt: str,
    timeout_seconds: float = 60.0,
    max_attempts: int = 3,
    wait: wait_base | None = None,
) -> Reviewer:
    structured = model.with_structured_output(ReviewResult, method="json_schema")

    async def call_once(messages: list[BaseMessage]) -> ReviewResult:
        """One model call, retried with exponential backoff on transient errors only."""
        async for attempt in AsyncRetrying(
            retry=retry_if_exception_type(_TRANSIENT),
            wait=wait or wait_exponential(multiplier=1, max=20),
            stop=stop_after_attempt(max_attempts),
            reraise=True,
        ):
            with attempt:
                async with asyncio.timeout(timeout_seconds):
                    raw = await structured.ainvoke(messages)
                if not isinstance(raw, ReviewResult):
                    raise ReviewFailed(f"unexpected model output type: {type(raw).__name__}")
                return raw
        raise AssertionError("unreachable")  # pragma: no cover

    async def review(path: str, numbered_diff: str) -> list[Finding]:
        messages: list[BaseMessage] = [
            SystemMessage(content=prompt),
            HumanMessage(content=_user_message(path, numbered_diff)),
        ]
        # Unparseable output: ask once more, then give up on this file.
        for output_attempt in (1, 2):
            try:
                return (await call_once(messages)).findings
            except (*_BAD_OUTPUT, ReviewFailed) as exc:
                logger.warning(
                    "bad model output for %r (attempt %d): %s", path, output_attempt, exc
                )
        raise ReviewFailed("model output could not be parsed after 2 attempts")

    return review
