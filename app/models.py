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
    theme: str = Field(default="warm", max_length=20)
    owner_language: str = Field(default="en", max_length=5)
    status: str = Field(default="draft", max_length=20)
    is_sample: bool = Field(default=False, index=True)
    source: str = Field(default="intake", max_length=10)  # seed (demo samples) or intake
    first_live_at: datetime | None = None
    consent_given_at: datetime | None = None
    consent_method: str | None = Field(default=None, max_length=20)
    consent_text: str | None = Field(default=None, sa_column=Column(Text, nullable=True))
    owner_link_hash: str | None = Field(default=None, unique=True, max_length=128)
    pin_hash: str | None = Field(default=None, max_length=256)
    pin_failures: int = 0
    pin_locked_until: datetime | None = None
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
    """One intake: what Claude first guessed, what the owner said, and what she corrected.

    first_guess is Claude's validated answer before the code-side checks; draft is what the
    confirmation cards start from. The transcript is kept here for accuracy measurement only
    and never logged. input_path points at audio or a photo waiting to be processed; it is
    deleted after processing unless the owner agreed to keep it (keep_input).
    """

    id: int | None = Field(default=None, primary_key=True)
    business_id: int | None = Field(default=None, foreign_key="business.id", index=True)
    token_hash: str | None = Field(default=None, unique=True, max_length=64)
    source: str = Field(max_length=20)
    language: str = Field(default="en", max_length=5)
    status: str = Field(default="queued", max_length=20)
    error_key: str | None = Field(default=None, max_length=40)
    job_id: str | None = Field(default=None, max_length=32)
    keep_input: bool = False
    input_path: str | None = Field(default=None, max_length=300)
    consent_text: str | None = Field(default=None, sa_column=Column(Text, nullable=True))
    transcript: str | None = Field(default=None, sa_column=Column(Text, nullable=True))
    stt_route: str | None = Field(default=None, max_length=10)
    seconds_to_draft_scored: int | None = None  # system time: submit -> first draft scored
    seconds_to_live: int | None = None  # submit -> first publish (includes her decision time)
    first_guess: dict[str, Any] | None = Field(default=None, sa_column=Column(JSON, nullable=True))
    draft: dict[str, Any] | None = Field(default=None, sa_column=Column(JSON, nullable=True))
    web_address: str | None = Field(default=None, max_length=63)
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
    """A customer enquiry, or a main-site contact message when business_id is empty.

    The full phone is stored so the owner can call back; logs redact it. notified_at is set when
    the notification email has been sent.
    """

    id: int | None = Field(default=None, primary_key=True)
    business_id: int | None = Field(default=None, foreign_key="business.id", index=True)
    name: str = Field(max_length=120)
    phone: str | None = Field(default=None, max_length=20)
    email: str | None = Field(default=None, max_length=254)
    message: str = Field(sa_column=Column(Text, nullable=False))
    status: str = Field(default="new", max_length=20)
    notified_at: datetime | None = None
    created_at: datetime = Field(default_factory=utcnow)


class ClaudeUsage(SQLModel, table=True):
    """Tokens and estimated cost of one Claude call, for cost per business and the daily cap."""

    id: int | None = Field(default=None, primary_key=True)
    business_id: int | None = Field(default=None, foreign_key="business.id", index=True)
    intake_id: int | None = Field(default=None, foreign_key="intakesession.id", index=True)
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


class Job(SQLModel, table=True):
    """A background job (for example a customer-test run) that the owner app polls."""

    id: str = Field(primary_key=True, max_length=32)
    kind: str = Field(max_length=30, index=True)
    business_id: int | None = Field(default=None, foreign_key="business.id", index=True)
    status: str = Field(default="queued", max_length=20, index=True)
    attempts: int = 0
    max_attempts: int = 2
    timeout_seconds: int = 300
    payload: dict[str, Any] | None = Field(default=None, sa_column=Column(JSON, nullable=True))
    error: str | None = Field(default=None, max_length=60)
    run_id: int | None = None
    created_at: datetime = Field(default_factory=utcnow)
    started_at: datetime | None = None
    finished_at: datetime | None = None


class SiteVersion(SQLModel, table=True):
    """One published version of a business site. Every publish is kept, which powers undo."""

    id: int | None = Field(default=None, primary_key=True)
    business_id: int = Field(foreign_key="business.id", index=True)
    number: int
    profile_version_id: int | None = Field(default=None, foreign_key="profileversion.id")
    theme: str = Field(max_length=20)
    site_hash: str = Field(max_length=64)
    status: str = Field(default="live", max_length=12)  # live, superseded or undone
    approved_at: datetime = Field(default_factory=utcnow)
    published_at: datetime = Field(default_factory=utcnow)


class TestRun(SQLModel, table=True):
    """One customer-test run of one version of a site, with its Readiness Score."""

    __test__ = False  # not a pytest test class

    id: int | None = Field(default=None, primary_key=True)
    business_id: int = Field(foreign_key="business.id", index=True)
    job_id: str | None = Field(default=None, foreign_key="job.id", index=True)
    profile_version_id: int | None = Field(default=None, foreign_key="profileversion.id")
    site_hash: str = Field(max_length=64)
    site_kind: str = Field(default="live", max_length=10)
    site_version_id: int | None = Field(default=None, foreign_key="siteversion.id", index=True)
    url: str = Field(max_length=300)
    total: int = 0
    possible_so_far: float = 0.0
    pending_points: float = 0.0
    screenshot_path: str | None = Field(default=None, max_length=300)
    duration_seconds: float = 0.0
    ai_status: str | None = Field(default=None, max_length=16)
    ai_model: str | None = Field(default=None, max_length=60)
    ai_cost_usd_estimate: float = 0.0
    created_at: datetime = Field(default_factory=utcnow)


class ScoreItem(SQLModel, table=True):
    """One scored check in a test run. source is measured or ai_assessed (rule 8)."""

    id: int | None = Field(default=None, primary_key=True)
    run_id: int = Field(foreign_key="testrun.id", index=True)
    part: str = Field(max_length=20)
    key: str = Field(max_length=40)
    source: str = Field(max_length=12)
    status: str = Field(max_length=16)
    points: float = 0.0
    max_points: float = 0.0
    reason_en: str = Field(sa_column=Column(Text, nullable=False))
    reason_hi: str = Field(sa_column=Column(Text, nullable=False))
    reason_mr: str = Field(sa_column=Column(Text, nullable=False))
    detail: dict[str, Any] | None = Field(default=None, sa_column=Column(JSON, nullable=True))


class SttUsage(SQLModel, table=True):
    """Seconds of audio transcribed and the estimated cost (INR), per business."""

    id: int | None = Field(default=None, primary_key=True)
    business_id: int | None = Field(default=None, foreign_key="business.id", index=True)
    intake_id: int | None = Field(default=None, foreign_key="intakesession.id", index=True)
    provider: str = Field(max_length=20)
    model: str = Field(max_length=40)
    route: str = Field(max_length=10)
    seconds: float = 0.0
    cost_inr_estimate: float = 0.0
    is_mock: bool = False
    day_ist: date = Field(index=True)
    created_at: datetime = Field(default_factory=utcnow)


class Photo(SQLModel, table=True):
    """An owner's photo (re-encoded, no location data). subject is a product name or None."""

    id: int | None = Field(default=None, primary_key=True)
    business_id: int = Field(foreign_key="business.id", index=True)
    filename: str = Field(max_length=80)
    subject: str | None = Field(default=None, max_length=120)
    is_main: bool = False
    created_at: datetime = Field(default_factory=utcnow)


class OwnerDevice(SQLModel, table=True):
    """A device the owner chose to stay signed in on (30 days). Only the token's hash is kept."""

    id: int | None = Field(default=None, primary_key=True)
    business_id: int = Field(foreign_key="business.id", index=True)
    token_hash: str = Field(unique=True, max_length=64)
    created_at: datetime = Field(default_factory=utcnow)
    expires_at: datetime
    last_used_at: datetime | None = None
    revoked_at: datetime | None = None
