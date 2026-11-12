"""The real Postgres checkpointer: tables, pause/resume across connections, deletion.

Needs TEST_DATABASE_URL (the database is wiped!), like tests/test_queue_postgres.py.
"""

import asyncio
from collections.abc import Coroutine
from typing import Any, TypedDict

import pytest
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt
from sqlalchemy import create_engine, text

from alembic import command
from app.config import get_settings
from app.review.checkpoint import delete_thread, open_checkpointer, setup_checkpointer
from tests.test_queue_postgres import URL, _alembic, pg  # noqa: F401  (pg is a fixture)

pytestmark = [
    pytest.mark.postgres,
    pytest.mark.skipif(not URL, reason="TEST_DATABASE_URL is not set"),
    pytest.mark.usefixtures("pg"),
]


class State(TypedDict):
    approved: bool


def _graph(saver: Any) -> Any:
    def hold(state: State) -> dict[str, bool]:
        return {"approved": bool(interrupt({"held": 1}))}

    graph = StateGraph(State)
    graph.add_node("hold", hold)
    graph.add_edge(START, "hold")
    graph.add_edge("hold", END)
    return graph.compile(checkpointer=saver)


def _run(coro: Coroutine[Any, Any, Any]) -> Any:
    # psycopg's async mode cannot use Windows' default Proactor loop; Linux (CI, ECS) is fine.
    return asyncio.run(coro, loop_factory=asyncio.SelectorEventLoop)


def _rows() -> int:
    assert URL
    engine = create_engine(URL)
    with engine.connect() as conn:
        count = int(conn.execute(text("SELECT count(*) FROM checkpoints")).scalar_one())
    engine.dispose()
    return count


def test_pause_survives_a_new_connection_then_resumes_and_is_deleted() -> None:
    settings = get_settings()
    config = {"configurable": {"thread_id": "run-1"}}

    async def review_job() -> Any:
        await setup_checkpointer(settings)
        await setup_checkpointer(settings)  # idempotent
        async with open_checkpointer(settings) as saver:
            return await _graph(saver).ainvoke({"approved": False}, config)

    async def confirm_job() -> bool:
        async with open_checkpointer(settings) as saver:  # a different connection
            final = await _graph(saver).ainvoke(Command(resume=True), config)
            assert await delete_thread(saver, "run-1") is True
            return bool(final["approved"])

    assert "__interrupt__" in _run(review_job())
    assert _rows() > 0
    assert _run(confirm_job()) is True
    assert _rows() == 0


def test_alembic_ignores_the_checkpointers_tables(monkeypatch: pytest.MonkeyPatch) -> None:
    _run(setup_checkpointer(get_settings()))
    command.check(_alembic(monkeypatch))  # would raise if autogenerate wanted to drop them
