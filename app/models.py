"""Database tables. Every change here needs a new Alembic migration (make migration msg=...)."""

from datetime import UTC, date, datetime
from typing import Any

from sqlalchemy import JSON, Column, Text, UniqueConstraint
from sqlmodel import Field, SQLModel


def utcnow() -> datetime:
    """Timezone-aware current time in UTC."""
    return datetime.now(UTC)


class Business(SQLModel, table=True):
    """A small business. Real businesses need recorded consent; samples are labelled everywhere."""

    id: int | None = Field(default=None, primary_key=True)
    slug: str = Field(index=True, unique=True, max_length=63)
    display_name: str = Field(max_length=120)
    business_type: str = Field(default="other", max_length=20)
    owner_language: str = Field(default="en", max_length=5)
    status: str = Field(default="draft", max_length=20)
    is_sample: bool = Field(default=False, index=True)
    consent_given_at: datetime | None = None
    consent_method: str | None = Field(default=None, max_length=20)
    consent_text: str | None = Field(default=None, sa_column=Column(Text, nullable=True))
    owner_link_hash: str | None = Field(default=None, unique=True, max_length=128)
    pin_hash: str | None = Field(default=None, max_length=256)
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)
    deleted_at: datetime | None = None


class ProfileVersion(SQLModel, table=True):
    """One saved version of a business profile. Every version is kept; this powers undo."""

    __table_args__ = (UniqueConstraint("business_id", "version"),)

    id: int | None = Field(default=None, primary_key=True)
    business_id: int = Field(foreign_key="business.id", index=True)
    version: int
    profile: dict[str, Any] = Field(sa_column=Column(JSON, nullable=False))
    created_by: str = Field(max_length=20)
    note: str | None = Field(default=None, max_length=300)
    created_at: datetime = Field(default_factory=utcnow)


class IntakeSession(SQLModel, table=True):
    """One intake: what Claude first guessed, what the owner said, and what she corrected."""

    id: int | None = Field(default=None, primary_key=True)
    business_id: int | None = Field(default=None, foreign_key="business.id", index=True)
    source: str = Field(max_length=20)
    language: str = Field(default="en", max_length=5)
    transcript: str | None = Field(default=None, sa_column=Column(Text, nullable=True))
    first_guess: dict[str, Any] | None = Field(default=None, sa_column=Column(JSON, nullable=True))
    corrections: list[Any] | None = Field(default=None, sa_column=Column(JSON, nullable=True))
    created_at: datetime = Field(default_factory=utcnow)
    completed_at: datetime | None = None


class ActionLog(SQLModel, table=True):
    """One agent step: what failed, what Claude decided, which tool it used, what changed."""

    id: int | None = Field(default=None, primary_key=True)
    business_id: int = Field(foreign_key="business.id", index=True)
    run_id: str = Field(index=True, max_length=40)
    round: int = 0
    step: str = Field(max_length=40)
    tool: str | None = Field(default=None, max_length=40)
    decision: str | None = Field(default=None, sa_column=Column(Text, nullable=True))
    detail: dict[str, Any] | None = Field(default=None, sa_column=Column(JSON, nullable=True))
    ok: bool = True
    created_at: datetime = Field(default_factory=utcnow)


class Enquiry(SQLModel, table=True):
    """A customer enquiry. The full phone is stored so the owner can call back; logs redact it."""

    id: int | None = Field(default=None, primary_key=True)
    business_id: int | None = Field(default=None, foreign_key="business.id", index=True)
    name: str = Field(max_length=120)
    phone: str | None = Field(default=None, max_length=20)
    message: str = Field(sa_column=Column(Text, nullable=False))
    status: str = Field(default="new", max_length=20)
    created_at: datetime = Field(default_factory=utcnow)


class ClaudeUsage(SQLModel, table=True):
    """Tokens and estimated cost of one Claude call, for cost per business and the daily cap."""

    id: int | None = Field(default=None, primary_key=True)
    business_id: int | None = Field(default=None, foreign_key="business.id", index=True)
    purpose: str = Field(max_length=60)
    model: str = Field(max_length=60)
    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_tokens: int = 0
    cache_read_tokens: int = 0
    cost_usd_estimate: float = 0.0
    is_mock: bool = False
    day_ist: date = Field(index=True)
    created_at: datetime = Field(default_factory=utcnow)
