"""LangGraph checkpointer helpers (Phase 8). Edge code: this is where Postgres is touched.

Checkpoints hold the numbered diffs and the findings of a PR (source code!). Delete a thread as soon
as its run is posted, expired or superseded, and never log what is inside.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any
from urllib.parse import parse_qsl, quote, urlencode, urlsplit, urlunsplit

import structlog
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer

from app.config import Settings

logger = structlog.get_logger()

# The only application types that may be read back from a checkpoint (msgpack allow-list).
# Anything else is BLOCKED, so a tampered checkpoint cannot make us build arbitrary objects.
# If the graph state gains a new custom type, add it here (a test pauses a real graph).
CHECKPOINT_TYPES: list[tuple[str, str]] = [
    ("app.diff.parse", "ParsedFile"),
    ("app.review.schemas", "Finding"),
    ("app.review.schemas", "ReviewFinding"),
    ("app.review.schemas", "Severity"),
    ("app.review.schemas", "Category"),
    ("app.review.schemas", "Agent"),
]


def checkpoint_serde() -> JsonPlusSerializer:
    return JsonPlusSerializer(allowed_msgpack_modules=CHECKPOINT_TYPES)


def checkpoint_conninfo(settings: Settings) -> str:
    """The libpq URL with the same connect and statement timeouts the rest of the app uses."""
    parts = urlsplit(settings.psycopg_url)
    query = dict(parse_qsl(parts.query))
    query.setdefault("connect_timeout", str(settings.db_connect_timeout_seconds))
    query.setdefault("options", f"-c statement_timeout={settings.db_statement_timeout_ms}")
    return urlunsplit(parts._replace(query=urlencode(query, quote_via=quote)))


@asynccontextmanager
async def open_checkpointer(settings: Settings) -> AsyncIterator[AsyncPostgresSaver]:
    async with AsyncPostgresSaver.from_conn_string(
        checkpoint_conninfo(settings), serde=checkpoint_serde()
    ) as saver:
        yield saver


async def setup_checkpointer(settings: Settings) -> None:
    """Create the checkpointer's own tables. Idempotent; call once when the worker starts."""
    async with open_checkpointer(settings) as saver:
        await saver.setup()


async def delete_thread(saver: BaseCheckpointSaver[Any], run_id: str) -> bool:
    """Remove everything checkpointed for a run. Best effort: never breaks a posted review.

    Returns False when deletion failed (logged by error type only, never by content).
    """
    try:
        await saver.adelete_thread(run_id)
    except Exception as exc:
        logger.warning("checkpoint_delete_failed", error=type(exc).__name__)
        return False
    return True
