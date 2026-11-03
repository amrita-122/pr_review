import asyncio
import base64
from collections.abc import Awaitable, Callable, Iterator

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.config import Settings, get_settings
from app.db.models import Base
from app.github import auth
from app.github.webhooks import PullRequestEvent
from app.jobs.queue import enqueue_review
from app.jobs.worker import run_worker
from app.main import app, get_db
from app.review.fake import review_file
from app.review.schemas import Finding
from tests.helpers import PRIVATE_PEM

TEST_SECRET = "test-webhook-secret"  # noqa: S105


@pytest.fixture(autouse=True)
def webhook_secret(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", TEST_SECRET)
    monkeypatch.setenv("GITHUB_APP_ID", "12345")
    monkeypatch.setenv("DATABASE_URL", "sqlite://")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "unused")
    monkeypatch.setenv("MODEL_REVIEW", "test-model")
    # Never read the developer's real .env in unit tests.
    monkeypatch.setitem(Settings.model_config, "env_file", None)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def github_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """A throwaway App private key, so installation-token minting works against respx mocks."""
    monkeypatch.setenv("GITHUB_PRIVATE_KEY_BASE64", base64.b64encode(PRIVATE_PEM.encode()).decode())
    get_settings.cache_clear()
    auth._token_cache.clear()
    yield
    get_settings.cache_clear()
    auth._token_cache.clear()


@pytest.fixture(autouse=True)
def db_session(monkeypatch: pytest.MonkeyPatch) -> Iterator[Session]:
    """In-memory SQLite stands in for Postgres in unit tests."""
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine, expire_on_commit=False)

    def override() -> Iterator[Session]:
        with factory() as session:
            yield session

    app.dependency_overrides[get_db] = override
    monkeypatch.setattr("app.jobs.handlers.get_session", factory)  # run records -> test DB
    monkeypatch.setattr("app.jobs.worker.get_session", factory)  # queue -> test DB
    with factory() as session:
        yield session
    app.dependency_overrides.clear()


@pytest.fixture
def enqueue(db_session: Session) -> Callable[[PullRequestEvent], None]:
    """Queue a review exactly as the webhook does (run + job), then commit."""

    def _enqueue(event: PullRequestEvent) -> None:
        enqueue_review(db_session, event, get_settings())
        db_session.commit()

    return _enqueue


@pytest.fixture
def drain() -> Callable[[], Awaitable[None]]:
    """Run the worker until no job is due. Failed jobs rescheduled into the future stay queued."""

    async def _drain() -> None:
        await run_worker(get_settings(), asyncio.Event(), once=True)

    return _drain


@pytest.fixture(autouse=True)
def fake_reviewer(monkeypatch: pytest.MonkeyPatch) -> None:
    """Unit tests must never call a real LLM: use the regex fake behind the async interface."""

    async def reviewer(path: str, numbered: str) -> list[Finding]:
        return review_file(path, numbered)

    monkeypatch.setattr("app.jobs.handlers.build_reviewer", lambda: reviewer)
