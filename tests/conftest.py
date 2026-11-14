"""Shared fixtures. Tests run in mock mode against a fresh, migrated SQLite database."""

import os
import secrets
from collections.abc import Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.engine import Engine

ROOT = Path(__file__).resolve().parents[1]
# Generated per run so no secret-looking values are ever committed.
TEST_SECRET = secrets.token_urlsafe(48)
TEST_ANTHROPIC_KEY = "sk-ant-" + secrets.token_urlsafe(24)
TEST_SMTP_PASSWORD = secrets.token_urlsafe(18)
TEST_ENV = {
    "APP_NAME": "Test App",
    "BASE_DOMAIN": "example.test",
    "APP_ENV": "test",
    "SECRET_KEY": TEST_SECRET,
    "CLAUDE_MOCK": "1",
    "CLAUDE_DAILY_SPEND_CAP_USD": "1.0",
    "ANTHROPIC_API_KEY": TEST_ANTHROPIC_KEY,
    "SMTP_PASSWORD": TEST_SMTP_PASSWORD,
}


def clear_caches() -> None:
    """Forget cached settings and engine so a changed environment takes effect."""
    from app.config import get_settings
    from app.db import get_engine

    get_settings.cache_clear()
    get_engine.cache_clear()


def run_migrations(database_url: str) -> None:
    """Upgrade a database to head with Alembic, the same way production does."""
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.attributes["database_url"] = database_url
    cfg.attributes["configure_logger"] = False
    command.upgrade(cfg, "head")


@pytest.fixture(autouse=True)
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    """Test environment with a migrated temp database; yields the database URL."""
    for key in list(os.environ):
        if key.startswith(("SMTP_", "GDDY_", "STT_")):
            monkeypatch.delenv(key, raising=False)
    database_url = f"sqlite:///{tmp_path / 'test.db'}"
    for key, value in {**TEST_ENV, "DATABASE_URL": database_url}.items():
        monkeypatch.setenv(key, value)
    monkeypatch.chdir(tmp_path)  # never read the developer's real .env
    clear_caches()
    run_migrations(database_url)
    from app.services import ratelimit

    ratelimit.reset()
    yield database_url
    from app.db import get_engine

    if get_engine.cache_info().currsize:
        get_engine().dispose()
    clear_caches()


@pytest.fixture
def engine(env: str) -> Engine:
    """The app's engine for the test database."""
    from app.db import get_engine

    return get_engine()


@pytest.fixture
def app(env: str) -> FastAPI:
    """A fresh app instance."""
    from app.main import create_app

    return create_app()


@pytest.fixture
def client(app: FastAPI) -> Iterator[TestClient]:
    """Test client that returns 500 pages instead of raising."""
    with TestClient(app, raise_server_exceptions=False) as test_client:
        yield test_client
