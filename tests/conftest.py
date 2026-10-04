from collections.abc import Iterator

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.config import get_settings
from app.db.models import Base
from app.github.webhooks import PullRequestEvent
from app.main import app, get_db

TEST_SECRET = "test-webhook-secret"  # noqa: S105


@pytest.fixture(autouse=True)
def webhook_secret(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", TEST_SECRET)
    monkeypatch.setenv("GITHUB_APP_ID", "12345")
    monkeypatch.setenv("GITHUB_PRIVATE_KEY_BASE64", "unused")
    monkeypatch.setenv("DATABASE_URL", "sqlite://")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def db_session() -> Iterator[Session]:
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
    with factory() as session:
        yield session
    app.dependency_overrides.clear()


@pytest.fixture(autouse=True)
def processed(monkeypatch: pytest.MonkeyPatch) -> list[PullRequestEvent]:
    """Replaces the background job so unit tests never touch GitHub."""
    calls: list[PullRequestEvent] = []

    async def fake(event: PullRequestEvent) -> None:
        calls.append(event)

    monkeypatch.setattr("app.main.process_pull_request", fake)
    return calls
