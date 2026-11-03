from functools import lru_cache

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.config import get_settings


@lru_cache
def get_engine() -> Engine:
    settings = get_settings()
    url = settings.sqlalchemy_url
    connect_args: dict[str, object] = {}
    if url.startswith("postgresql"):
        # A dead or stuck database must fail a job quickly, not hang it.
        connect_args = {
            "connect_timeout": settings.db_connect_timeout_seconds,
            "options": f"-c statement_timeout={settings.db_statement_timeout_ms}",
        }
    return create_engine(url, pool_pre_ping=True, connect_args=connect_args)


def get_session() -> Session:
    return sessionmaker(get_engine(), expire_on_commit=False)()
