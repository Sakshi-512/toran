"""Shared fixtures. Tests run in mock mode against a fresh, migrated SQLite database."""

import os
import secrets
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.engine import Engine

from app.models import Business

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
    "GDDY_MOCK": "1",
    "CLAUDE_DAILY_SPEND_CAP_USD": "1.0",
    "ANTHROPIC_API_KEY": TEST_ANTHROPIC_KEY,
    "SMTP_PASSWORD": TEST_SMTP_PASSWORD,
    "CONTACT_TO_EMAIL": "team@example.test",
}


def clear_caches() -> None:
    """Forget cached settings and engine so a changed environment takes effect."""
    from app.config import get_settings
    from app.db import get_engine

    get_settings.cache_clear()
    get_engine.cache_clear()
    from app.routes.dev import _preview_build

    _preview_build.cache_clear()


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
    paths = {
        "DATABASE_URL": database_url,
        "SITES_DIR": str(tmp_path / "sites"),
        "LOG_DIR": str(tmp_path / "logs"),
    }
    for key, value in {**TEST_ENV, **paths}.items():
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


@pytest.fixture
def seed_sample(engine: Engine) -> Callable[[str], Business]:
    """Create a sample business in the database and build its site into SITES_DIR."""
    from datetime import date

    from sqlmodel import Session

    from app.config import get_settings
    from app.services.samples import get_sample
    from app.services.sites import build_business_site
    from scripts.seed_samples import upsert_sample

    def seed(slug: str) -> Business:
        sample = get_sample(slug)
        assert sample is not None
        with Session(engine) as session:
            business = upsert_sample(session, sample)
            session.refresh(business)
            session.expunge(business)
        build_business_site(
            business, sample.profile, get_settings(), date(2026, 11, 14), sample.photos
        )
        return business

    return seed


@pytest.fixture(autouse=True)
def no_real_email(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Tests never send real email: SMTP connections fail loudly; the outbox starts empty."""
    import smtplib

    from app.services import mailer

    def refuse(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("a test tried to open a real SMTP connection")

    monkeypatch.setattr(smtplib, "SMTP", refuse)
    monkeypatch.setattr(smtplib, "SMTP_SSL", refuse)
    mailer.outbox.outbox.clear()
    yield
    mailer.outbox.outbox.clear()
