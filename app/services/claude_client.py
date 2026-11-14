"""All Claude calls go through ClaudeClient.

It adds: a required max_tokens, a timeout, retry with backoff, prompt caching of the system
prompt, token and estimated-cost logging per business, a daily spend cap (Asia/Kolkata days)
and mock mode (CLAUDE_MOCK=1 reads tests/fixtures/claude/<fixture_key>.json, no network).
"""

import json
import logging
import random
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any, Protocol
from zoneinfo import ZoneInfo

import anthropic
from sqlalchemy import func
from sqlalchemy.engine import Engine
from sqlmodel import Session, select

from app.config import Settings
from app.models import ClaudeUsage
from app.services.errors import SpendCapReached

logger = logging.getLogger(__name__)

IST = ZoneInfo("Asia/Kolkata")
FIXTURE_DIR = Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "claude"

HAIKU = "claude-haiku-5-5"
SONNET = "claude-sonnet-5-5"
OPUS = "claude-opus-5-5"

# Estimated USD per million tokens (input, output), from the hackathon deck. Estimates only.
PRICES_PER_MTOK: dict[str, tuple[float, float]] = {
    HAIKU: (0.10, 0.50),
    SONNET: (2.00, 10.00),
    OPUS: (4.00, 20.00),
}
CACHE_WRITE_MULTIPLIER = 1.25
CACHE_READ_MULTIPLIER = 0.10

DEFAULT_TIMEOUT_SECONDS = 60.0
MAX_ATTEMPTS = 4
BACKOFF_BASE_SECONDS = 1.0
BACKOFF_MAX_SECONDS = 20.0

RETRYABLE_ERRORS: tuple[type[Exception], ...] = (
    anthropic.APIConnectionError,
    anthropic.RateLimitError,
    anthropic.InternalServerError,
)


class ClaudeUnavailable(Exception):
    """Claude could not be reached after retries, or is not configured."""


@dataclass(frozen=True)
class Usage:
    """Token counts reported for one call."""

    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_tokens: int = 0
    cache_read_tokens: int = 0


@dataclass(frozen=True)
class ClaudeResult:
    """Text reply plus usage and estimated cost of one call."""

    text: str
    model: str
    usage: Usage
    cost_usd_estimate: float
    is_mock: bool


class Transport(Protocol):
    """Sends one Messages API request. Swappable for tests and mock mode."""

    def send(self, request: dict[str, Any], fixture_key: str) -> tuple[str, Usage]:
        """Return (text, usage) for the request."""
        ...


class AnthropicTransport:
    """Real transport using the official SDK. SDK retries are off; ClaudeClient retries."""

    def __init__(self, api_key: str, timeout_seconds: float) -> None:
        self._client = anthropic.Anthropic(api_key=api_key, timeout=timeout_seconds, max_retries=0)

    def send(self, request: dict[str, Any], fixture_key: str) -> tuple[str, Usage]:
        """Call messages.create and return the joined text blocks and usage."""
        message = self._client.messages.create(**request)
        text = "".join(getattr(block, "text", "") for block in message.content)
        u = message.usage
        return text, Usage(
            input_tokens=u.input_tokens or 0,
            output_tokens=u.output_tokens or 0,
            cache_creation_tokens=u.cache_creation_input_tokens or 0,
            cache_read_tokens=u.cache_read_input_tokens or 0,
        )


class MockTransport:
    """Returns saved responses from tests/fixtures/claude/<fixture_key>.json."""

    def __init__(self, fixture_dir: Path = FIXTURE_DIR) -> None:
        self._dir = fixture_dir

    def send(self, request: dict[str, Any], fixture_key: str) -> tuple[str, Usage]:
        """Load the fixture; fails clearly if it is missing."""
        path = self._dir / f"{fixture_key}.json"
        if not path.resolve().is_relative_to(self._dir.resolve()):
            raise ValueError("fixture_key must not leave the fixture directory")
        if not path.exists():
            raise ClaudeUnavailable(f"Mock fixture not found: {path.name}")
        data = json.loads(path.read_text(encoding="utf-8"))
        usage = data.get("usage", {})
        return str(data["text"]), Usage(
            input_tokens=int(usage.get("input_tokens", 0)),
            output_tokens=int(usage.get("output_tokens", 0)),
            cache_creation_tokens=int(usage.get("cache_creation_input_tokens", 0)),
            cache_read_tokens=int(usage.get("cache_read_input_tokens", 0)),
        )


def estimate_cost(model: str, usage: Usage) -> float:
    """Estimated USD cost of one call. Unknown models use the most expensive known price."""
    input_price, output_price = PRICES_PER_MTOK.get(model, max(PRICES_PER_MTOK.values()))
    input_cost = (
        usage.input_tokens
        + usage.cache_creation_tokens * CACHE_WRITE_MULTIPLIER
        + usage.cache_read_tokens * CACHE_READ_MULTIPLIER
    ) * input_price
    return round((input_cost + usage.output_tokens * output_price) / 1_000_000, 6)


def today_ist(now: datetime | None = None) -> date:
    """The current calendar day in Asia/Kolkata, which is the spend cap's day."""
    return (now or datetime.now(IST)).astimezone(IST).date()


def build_transport(settings: Settings, timeout_seconds: float) -> Transport:
    """Mock transport when CLAUDE_MOCK=1, otherwise the real SDK."""
    if settings.claude_mock:
        return MockTransport()
    if settings.anthropic_api_key is None:
        raise ClaudeUnavailable("ANTHROPIC_API_KEY is not set")
    return AnthropicTransport(settings.anthropic_api_key.get_secret_value(), timeout_seconds)


class ClaudeClient:
    """Makes Claude calls with retries, caching, cost logging and the daily spend cap."""

    def __init__(
        self,
        settings: Settings,
        engine: Engine,
        transport: Transport | None = None,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], datetime] = lambda: datetime.now(IST),
    ) -> None:
        self._settings = settings
        self._engine = engine
        self._timeout = timeout_seconds
        self._transport = transport
        self._sleep = sleep
        self._clock = clock

    @property
    def transport(self) -> Transport:
        """The transport, created lazily so the app starts without an API key."""
        if self._transport is None:
            self._transport = build_transport(self._settings, self._timeout)
        return self._transport

    def complete(
        self,
        *,
        model: str,
        system: str,
        messages: list[dict[str, Any]],
        max_tokens: int,
        purpose: str,
        business_id: int | None = None,
        fixture_key: str | None = None,
        cache_system: bool = True,
        temperature: float | None = None,
    ) -> ClaudeResult:
        """Send one request and return the reply. Raises SpendCapReached or ClaudeUnavailable."""
        if max_tokens <= 0:
            raise ValueError("max_tokens must be positive")
        is_mock = self._settings.claude_mock
        if not is_mock:
            self.check_spend_cap()
        request = self._build_request(model, system, messages, max_tokens, cache_system)
        if temperature is not None:
            request["temperature"] = temperature
        text, usage = self._send_with_retry(request, fixture_key or purpose)
        cost = estimate_cost(model, usage)
        self._record(model, usage, cost, purpose, business_id, is_mock)
        return ClaudeResult(text, model, usage, cost, is_mock)

    def spent_today(self) -> float:
        """Estimated USD spent on real (non-mock) calls today, Asia/Kolkata time."""
        with Session(self._engine) as session:
            statement = select(func.coalesce(func.sum(ClaudeUsage.cost_usd_estimate), 0.0)).where(
                ClaudeUsage.day_ist == today_ist(self._clock()),
                ClaudeUsage.is_mock == False,  # noqa: E712 - SQL expression
            )
            return float(session.exec(statement).one())

    def check_spend_cap(self) -> None:
        """Raise SpendCapReached if today's estimated spend has reached the cap."""
        spent = self.spent_today()
        cap = self._settings.claude_daily_spend_cap_usd
        if spent >= cap:
            logger.warning("claude_spend_cap_reached", extra={"spent_usd": spent, "cap_usd": cap})
            raise SpendCapReached(spent, cap)

    @staticmethod
    def _build_request(
        model: str,
        system: str,
        messages: list[dict[str, Any]],
        max_tokens: int,
        cache_system: bool,
    ) -> dict[str, Any]:
        system_block: dict[str, Any] = {"type": "text", "text": system}
        if cache_system:
            system_block["cache_control"] = {"type": "ephemeral"}
        return {
            "model": model,
            "max_tokens": max_tokens,
            "system": [system_block],
            "messages": messages,
        }

    def _send_with_retry(self, request: dict[str, Any], fixture_key: str) -> tuple[str, Usage]:
        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                return self.transport.send(request, fixture_key)
            except RETRYABLE_ERRORS as exc:
                if attempt == MAX_ATTEMPTS:
                    logger.error(
                        "claude_failed", extra={"attempts": attempt, "error": type(exc).__name__}
                    )
                    raise ClaudeUnavailable("Claude is not reachable right now") from exc
                delay = self._backoff(attempt)
                logger.warning(
                    "claude_retry",
                    extra={"attempt": attempt, "delay_s": delay, "error": type(exc).__name__},
                )
                self._sleep(delay)
        raise ClaudeUnavailable("unreachable")  # pragma: no cover

    @staticmethod
    def _backoff(attempt: int) -> float:
        base: float = min(BACKOFF_MAX_SECONDS, BACKOFF_BASE_SECONDS * 2 ** (attempt - 1))
        return round(base + random.uniform(0, base / 2), 2)  # noqa: S311 - jitter, not crypto

    def _record(
        self,
        model: str,
        usage: Usage,
        cost: float,
        purpose: str,
        business_id: int | None,
        is_mock: bool,
    ) -> None:
        row = ClaudeUsage(
            business_id=business_id,
            purpose=purpose,
            model=model,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            cache_creation_tokens=usage.cache_creation_tokens,
            cache_read_tokens=usage.cache_read_tokens,
            cost_usd_estimate=cost,
            is_mock=is_mock,
            day_ist=today_ist(self._clock()),
        )
        with Session(self._engine) as session:
            session.add(row)
            session.commit()
        logger.info(
            "claude_call",
            extra={
                "purpose": purpose,
                "model": model,
                "business_id": business_id,
                "input_tokens": usage.input_tokens,
                "output_tokens": usage.output_tokens,
                "cache_read_tokens": usage.cache_read_tokens,
                "cache_creation_tokens": usage.cache_creation_tokens,
                "cost_usd_estimate": cost,
                "mock": is_mock,
            },
        )
