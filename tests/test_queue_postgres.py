"""Queue behaviour that SQLite cannot show: row locking and the real migrations.

Needs a throwaway Postgres: TEST_DATABASE_URL=postgresql+psycopg://user:pw@host:port/dbname
(the database is wiped!). Skipped when the variable is not set; CI provides one.
"""

import os
import threading
from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from alembic.config import Config
from sqlalchemy import Engine, create_engine, func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from alembic import command
from app.config import get_settings
from app.db.models import Job, ReviewRun
from app.jobs import worker
from app.jobs.queue import enqueue_review
from tests.test_jobs import make_event

URL = os.environ.get("TEST_DATABASE_URL")
pytestmark = [
    pytest.mark.postgres,
    pytest.mark.skipif(not URL, reason="TEST_DATABASE_URL is not set"),
]


def _alembic(monkeypatch: pytest.MonkeyPatch) -> Config:
    assert URL
    monkeypatch.setenv("DATABASE_URL", URL)  # alembic/env.py reads it through Settings
    get_settings.cache_clear()
    return Config("alembic.ini")


@pytest.fixture
def pg(monkeypatch: pytest.MonkeyPatch) -> Iterator[Engine]:
    assert URL
    engine = create_engine(URL)
    with engine.begin() as conn:
        conn.execute(text("DROP SCHEMA public CASCADE"))
        conn.execute(text("CREATE SCHEMA public"))
    command.upgrade(_alembic(monkeypatch), "head")  # the real migrations, not create_all
    yield engine
    engine.dispose()


def test_migrations_match_the_models(pg: Engine, monkeypatch: pytest.MonkeyPatch) -> None:
    command.check(_alembic(monkeypatch))  # raises if a model change has no migration


def test_migration_keeps_the_newest_of_duplicate_runs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert URL
    engine = create_engine(URL)
    with engine.begin() as conn:
        conn.execute(text("DROP SCHEMA public CASCADE"))
        conn.execute(text("CREATE SCHEMA public"))
    cfg = _alembic(monkeypatch)
    command.upgrade(cfg, "a41c7e92d0b3")  # the schema before Phase 6
    with engine.begin() as conn:
        for run_id, started in (("old", "2026-01-01"), ("new", "2026-01-02")):
            conn.execute(
                text(
                    "INSERT INTO review_runs (id, repo, pr_number, head_sha, status,"
                    " prompt_version,"
                    " model, started_at, tokens_in, tokens_out, cost_usd) VALUES (:i, 'o/r', 1,"
                    " 'abc', 'ok', 'p', 'm', :s, 0, 0, 0)"
                ),
                {"i": run_id, "s": started},
            )
        conn.execute(
            text(
                "INSERT INTO findings (run_id, file, line, severity, category, title, posted)"
                " VALUES ('old', 'a.py', 1, 'high', 'other', 't', true)"
            )
        )
    command.upgrade(cfg, "head")
    with engine.connect() as conn:
        assert [r[0] for r in conn.execute(text("SELECT id FROM review_runs"))] == ["new"]
        assert conn.execute(text("SELECT count(*) FROM findings")).scalar() == 0
    engine.dispose()


def test_concurrent_workers_never_claim_the_same_job(pg: Engine) -> None:
    factory = sessionmaker(pg, expire_on_commit=False)
    with factory() as s:
        s.add_all(Job(kind="x", payload={"n": i}) for i in range(40))
        s.commit()

    claimed: list[int] = []
    lock = threading.Lock()

    def work(name: str) -> None:
        while True:
            with factory() as session:
                job = worker.claim_job(session, name, datetime.now(UTC))
            if job is None:
                return
            with lock:
                claimed.append(job.id)

    threads = [threading.Thread(target=work, args=(f"w{i}",)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(claimed) == 40 and len(set(claimed)) == 40  # every job once, none twice


def test_a_locked_job_is_skipped_not_waited_for(pg: Engine) -> None:
    factory = sessionmaker(pg, expire_on_commit=False)
    with factory() as s:
        s.add_all([Job(kind="x", payload={}), Job(kind="x", payload={})])
        s.commit()
    holder = factory()
    first = holder.scalars(
        select(Job).order_by(Job.id).limit(1).with_for_update()
    ).one()  # another worker is mid-claim: row lock held, not committed
    with factory() as other:
        job = worker.claim_job(other, "w2", datetime.now(UTC))
    assert job is not None and job.id != first.id  # skipped the locked row, took the next
    holder.rollback()
    holder.close()


def test_enqueue_is_atomic_supersedes_and_run_key_is_unique(pg: Engine) -> None:
    factory = sessionmaker(pg, expire_on_commit=False)
    settings = get_settings()
    with factory() as s:
        assert enqueue_review(s, make_event("a" * 40), settings)
        s.commit()
    with factory() as s:
        assert enqueue_review(s, make_event("b" * 40), settings)
        s.rollback()  # the webhook failed after enqueueing: nothing of it may survive
    with factory() as s:
        assert s.scalar(select(func.count()).select_from(Job)) == 1
        assert [r.status for r in s.scalars(select(ReviewRun))] == ["queued"]
    with factory() as s:
        assert enqueue_review(s, make_event("b" * 40), settings)
        s.commit()
    with factory() as s:
        statuses = {r.head_sha[0]: r.status for r in s.scalars(select(ReviewRun))}
        assert statuses == {"a": "superseded", "b": "queued"}
    with factory() as s:  # the unique constraint is the last line of defence against duplicates
        s.add(
            ReviewRun(
                id="dup",
                repo="amrita-122/review-bot-sandbox",
                pr_number=make_event().number,
                head_sha="b" * 40,
                status="queued",
                prompt_version="p",
                model="m",
            )
        )
        with pytest.raises(IntegrityError):
            s.commit()


def test_reaper_works_on_postgres(pg: Engine) -> None:
    factory = sessionmaker(pg, expire_on_commit=False)
    with factory() as s:
        s.add(
            Job(
                kind="x",
                payload={},
                status="running",
                attempts=1,
                locked_at=datetime(2020, 1, 1, tzinfo=UTC),
            )
        )
        s.commit()
    with factory() as s:
        assert worker.requeue_stale(s, datetime.now(UTC), 600) == []
    with Session(pg) as s:
        assert s.scalars(select(Job)).one().status == "queued"
