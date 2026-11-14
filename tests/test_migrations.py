from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import inspect, text
from sqlalchemy.engine import Engine
from sqlmodel import SQLModel

import app.models  # noqa: F401


def test_all_tables_created_by_migration(engine: Engine) -> None:
    tables = set(inspect(engine).get_table_names())
    assert {
        "business",
        "profileversion",
        "intakesession",
        "actionlog",
        "enquiry",
        "claudeusage",
        "alembic_version",
    } <= tables


def test_models_match_migrations(engine: Engine) -> None:
    with engine.connect() as connection:
        diff = compare_metadata(MigrationContext.configure(connection), SQLModel.metadata)
    assert diff == []


def test_sqlite_runs_in_wal_mode(engine: Engine) -> None:
    with engine.connect() as connection:
        assert connection.execute(text("PRAGMA journal_mode")).scalar() == "wal"
        assert connection.execute(text("PRAGMA foreign_keys")).scalar() == 1


def test_business_has_sample_and_consent_fields(engine: Engine) -> None:
    columns = {c["name"] for c in inspect(engine).get_columns("business")}
    assert {"is_sample", "consent_given_at", "consent_text", "consent_method"} <= columns
    enquiry = {c["name"] for c in inspect(engine).get_columns("enquiry")}
    assert "phone" in enquiry


def test_app_code_never_calls_create_all() -> None:
    from tests.conftest import ROOT

    for path in (ROOT / "app").rglob("*.py"):
        assert "create_all" not in path.read_text(), path
