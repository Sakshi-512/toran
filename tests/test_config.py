import pytest
from pydantic import ValidationError

from app.config import Settings, get_settings


def test_reads_every_variable_from_environment() -> None:
    settings = get_settings()
    assert settings.app_name == "Test App"
    assert settings.base_domain == "example.test"
    assert settings.claude_mock is True
    assert settings.claude_daily_spend_cap_usd == 1.0
    names = {name.upper() for name in Settings.model_fields}
    required = {
        "APP_NAME",
        "BASE_DOMAIN",
        "SECRET_KEY",
        "DATABASE_URL",
        "ANTHROPIC_API_KEY",
        "CLAUDE_MOCK",
        "CLAUDE_DAILY_SPEND_CAP_USD",
        "STT_PROVIDER",
        "STT_API_KEY",
        "SMTP_HOST",
        "SMTP_PORT",
        "SMTP_USER",
        "SMTP_PASSWORD",
        "CONTACT_TO_EMAIL",
        "GDDY_PAT",
    }
    assert required <= names


def test_env_example_lists_every_setting() -> None:
    from tests.conftest import ROOT

    listed = {
        line.split("=")[0]
        for line in (ROOT / ".env.example").read_text().splitlines()
        if "=" in line and not line.startswith("#")
    }
    infra_only = {"ACME_EMAIL"}  # read by Caddy, not by the app
    assert {name.upper() for name in Settings.model_fields} | infra_only == listed
    assert all(
        line.endswith("=")
        for line in (ROOT / ".env.example").read_text().splitlines()
        if "=" in line and not line.startswith("#")
    )


def test_secrets_are_hidden_in_repr() -> None:
    settings = get_settings()
    from tests.conftest import TEST_ANTHROPIC_KEY, TEST_SMTP_PASSWORD

    assert TEST_ANTHROPIC_KEY not in repr(settings)
    assert TEST_SMTP_PASSWORD not in repr(settings)
    assert TEST_ANTHROPIC_KEY in settings.secret_values()


def test_short_secret_key_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SECRET_KEY", "short")
    with pytest.raises(ValidationError):
        Settings()


def test_cookie_secure_follows_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    assert get_settings().cookie_secure is False
    monkeypatch.setenv("APP_ENV", "production")
    assert Settings().cookie_secure is True
    monkeypatch.setenv("APP_ENV", "development")
    assert Settings().cookie_secure is False
