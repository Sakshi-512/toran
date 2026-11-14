"""Application settings, read only from environment variables (or a local .env file)."""

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

AppEnv = Literal["production", "development", "test"]


class Settings(BaseSettings):
    """Every setting the app uses. Secrets are SecretStr so they never print by accident."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_ignore_empty=True,
        extra="ignore",
        case_sensitive=False,
    )

    app_name: str = Field(min_length=1)
    base_domain: str = Field(min_length=3)
    app_env: AppEnv = "production"
    secret_key: SecretStr
    database_url: str = "sqlite:///./data/app.db"
    sites_dir: Path = Path("data/sites")
    log_dir: Path = Path("data/logs")

    anthropic_api_key: SecretStr | None = None
    claude_mock: bool = False
    claude_daily_spend_cap_usd: float = Field(default=5.0, gt=0)

    stt_provider: str = "none"
    stt_api_key: SecretStr | None = None

    smtp_host: str | None = None
    smtp_port: int = 587
    smtp_user: str | None = None
    smtp_password: SecretStr | None = None
    contact_to_email: str | None = None

    gddy_pat: SecretStr | None = None

    @field_validator("base_domain")
    @classmethod
    def _normalise_domain(cls, value: str) -> str:
        return value.strip().lower().rstrip(".")

    @field_validator("secret_key")
    @classmethod
    def _strong_secret(cls, value: SecretStr) -> SecretStr:
        if len(value.get_secret_value()) < 32:
            raise ValueError("SECRET_KEY must be at least 32 characters")
        return value

    @property
    def is_production(self) -> bool:
        """True when running for real users."""
        return self.app_env == "production"

    @property
    def cookie_secure(self) -> bool:
        """Secure cookie flag: on in production, off for http://localhost development and tests."""
        return self.is_production

    def secret_values(self) -> list[str]:
        """Plain values of every configured secret, used only by the log redactor."""
        values: list[str] = []
        for name in type(self).model_fields:
            field_value = getattr(self, name)
            if isinstance(field_value, SecretStr):
                plain = field_value.get_secret_value()
                if len(plain) >= 6:
                    values.append(plain)
        return values


@lru_cache
def get_settings() -> Settings:
    """Return the cached settings. Tests call get_settings.cache_clear() after changing env."""
    return Settings()
