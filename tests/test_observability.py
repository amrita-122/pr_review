import os

import pytest

from app.config import get_settings
from app.observability import configure_tracing
from app.review.graph import ReviewLimits, review_diff


def test_tracing_off_by_default_leaves_env_untouched(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LANGSMITH_TRACING", raising=False)
    assert configure_tracing(get_settings()) is False
    assert "LANGSMITH_TRACING" not in os.environ


def test_tracing_needs_key_and_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LANGSMITH_TRACING", "true")
    monkeypatch.delenv("LANGSMITH_API_KEY", raising=False)
    get_settings.cache_clear()
    assert configure_tracing(get_settings()) is False  # flag without a key stays off


def test_tracing_exports_env_when_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LANGSMITH_TRACING", "true")
    monkeypatch.setenv("LANGSMITH_API_KEY", "ls-test")
    monkeypatch.setenv("LANGSMITH_PROJECT", "proj")
    get_settings.cache_clear()
    # monkeypatch restores these afterwards, so nothing leaks into other tests
    for name in ("LANGSMITH_TRACING", "LANGSMITH_API_KEY", "LANGSMITH_PROJECT"):
        monkeypatch.setenv(name, os.environ[name])
    assert configure_tracing(get_settings()) is True
    assert os.environ["LANGSMITH_PROJECT"] == "proj"


async def test_review_diff_accepts_labels_without_changing_result() -> None:
    outcome = await review_diff(
        {},
        _never_called,
        ReviewLimits(max_files=1, max_chars_per_file=10),
        run_name="review o/r#1",
        metadata={"repo": "o/r"},
    )
    assert outcome.findings == []


async def _never_called(path: str, numbered: str) -> list:  # type: ignore[type-arg]
    raise AssertionError("must not be called")
