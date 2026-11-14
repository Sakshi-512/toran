"""Database engine and sessions. SQLite runs in WAL mode. Schema changes go through Alembic only."""

from collections.abc import Iterator
from functools import lru_cache
from pathlib import Path
from typing import Any

from sqlalchemy import event
from sqlalchemy.engine import Engine, make_url
from sqlmodel import Session, create_engine

from app.config import get_settings


def _ensure_sqlite_dir(database_url: str) -> None:
    url = make_url(database_url)
    if url.get_backend_name() == "sqlite" and url.database and url.database != ":memory:":
        Path(url.database).parent.mkdir(parents=True, exist_ok=True)


def _set_sqlite_pragmas(dbapi_connection: Any, _record: Any) -> None:
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.execute("PRAGMA busy_timeout=5000")
    cursor.execute("PRAGMA synchronous=NORMAL")
    cursor.close()


def build_engine(database_url: str) -> Engine:
    """Create an engine; for SQLite, turn on WAL, foreign keys and a busy timeout per connection."""
    _ensure_sqlite_dir(database_url)
    is_sqlite = make_url(database_url).get_backend_name() == "sqlite"
    connect_args = {"check_same_thread": False} if is_sqlite else {}
    engine = create_engine(database_url, connect_args=connect_args)
    if is_sqlite:
        event.listen(engine, "connect", _set_sqlite_pragmas)
    return engine


@lru_cache
def get_engine() -> Engine:
    """Return the shared engine for the configured DATABASE_URL."""
    return build_engine(get_settings().database_url)


def get_session() -> Iterator[Session]:
    """FastAPI dependency that yields one database session per request."""
    with Session(get_engine()) as session:
        yield session
