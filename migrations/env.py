"""Alembic environment. Uses DATABASE_URL from app settings and the SQLModel metadata."""

from logging.config import fileConfig

from alembic import context
from sqlmodel import SQLModel

import app.models  # noqa: F401 - registers tables on SQLModel.metadata
from app.config import get_settings
from app.db import build_engine

config = context.config
if config.config_file_name is not None and config.attributes.get("configure_logger", True):
    fileConfig(config.config_file_name, disable_existing_loggers=False)

target_metadata = SQLModel.metadata


def _url() -> str:
    return str(config.attributes.get("database_url") or get_settings().database_url)


def run_migrations_offline() -> None:
    """Emit SQL without a database connection."""
    context.configure(
        url=_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        render_as_batch=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations against the configured database (WAL mode is set on connect).

    SQLite batch migrations rebuild tables (copy, drop, rename), which fails while other tables
    point at them, so foreign keys are off during migrations and every link is checked after.
    """
    engine = build_engine(_url())
    with engine.connect() as connection:
        is_sqlite = connection.dialect.name == "sqlite"
        if is_sqlite:
            connection.exec_driver_sql("PRAGMA foreign_keys=OFF")
            connection.commit()
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            render_as_batch=True,
            compare_type=True,
        )
        with context.begin_transaction():
            context.run_migrations()
        if is_sqlite:
            broken = connection.exec_driver_sql("PRAGMA foreign_key_check").fetchall()
            connection.exec_driver_sql("PRAGMA foreign_keys=ON")
            connection.commit()
            if broken:
                raise RuntimeError(f"Migration left {len(broken)} broken foreign key links")
    engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
