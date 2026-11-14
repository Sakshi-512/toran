from datetime import datetime
from typing import Any

import anthropic
import httpx2
import pytest
from sqlalchemy.engine import Engine
from sqlmodel import Session, select

from app.config import get_settings
from app.models import ClaudeUsage
from app.services.claude_client import (
    HAIKU,
    IST,
    SONNET,
    ClaudeClient,
    ClaudeUnavailable,
    Usage,
    estimate_cost,
    today_ist,
)
from app.services.errors import SpendCapReached

MESSAGES = [{"role": "user", "content": "hello"}]


class FakeTransport:
    def __init__(self, failures: int = 0, usage: Usage | None = None) -> None:
        self.failures = failures
        self.calls: list[dict[str, Any]] = []
        self.usage = usage or Usage(input_tokens=1000, output_tokens=200)

    def send(self, request: dict[str, Any], fixture_key: str) -> tuple[str, Usage]:
        self.calls.append(request)
        if len(self.calls) <= self.failures:
            raise anthropic.APIConnectionError(request=httpx2.Request("POST", "https://x"))
        return "ok", self.usage


def _real_client(
    engine: Engine,
    transport: FakeTransport,
    monkeypatch: pytest.MonkeyPatch,
    clock: datetime | None = None,
) -> ClaudeClient:
    monkeypatch.setenv("CLAUDE_MOCK", "0")
    get_settings.cache_clear()
    return ClaudeClient(
        get_settings(),
        engine,
        transport=transport,
        sleep=lambda _s: None,
        clock=(lambda: clock) if clock else (lambda: datetime.now(IST)),
    )


def test_mock_mode_reads_fixture_and_logs_usage(engine: Engine) -> None:
    client = ClaudeClient(get_settings(), engine)
    result = client.complete(
        model=SONNET,
        system="sys",
        messages=MESSAGES,
        max_tokens=100,
        purpose="classify",
        fixture_key="example",
        business_id=None,
    )
    assert result.is_mock is True
    assert "bakery" in result.text
    with Session(engine) as session:
        row = session.exec(select(ClaudeUsage)).one()
    assert row.is_mock is True
    assert row.input_tokens == 1200
    assert row.cache_read_tokens == 900


def test_mock_mode_missing_fixture_is_clear(engine: Engine) -> None:
    client = ClaudeClient(get_settings(), engine)
    with pytest.raises(ClaudeUnavailable, match="fixture"):
        client.complete(
            model=HAIKU,
            system="s",
            messages=MESSAGES,
            max_tokens=10,
            purpose="x",
            fixture_key="does_not_exist",
        )


def test_mock_fixture_key_cannot_escape(engine: Engine) -> None:
    client = ClaudeClient(get_settings(), engine)
    with pytest.raises(ValueError):
        client.complete(
            model=HAIKU,
            system="s",
            messages=MESSAGES,
            max_tokens=10,
            purpose="x",
            fixture_key="../../app/config",
        )


def test_request_has_max_tokens_and_cached_system(
    engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    transport = FakeTransport()
    client = _real_client(engine, transport, monkeypatch)
    client.complete(model=HAIKU, system="be brief", messages=MESSAGES, max_tokens=50, purpose="t")
    request = transport.calls[0]
    assert request["max_tokens"] == 50
    assert request["system"][0]["cache_control"] == {"type": "ephemeral"}


def test_max_tokens_required_positive(engine: Engine) -> None:
    client = ClaudeClient(get_settings(), engine)
    with pytest.raises(ValueError):
        client.complete(model=HAIKU, system="s", messages=MESSAGES, max_tokens=0, purpose="x")


def test_retries_then_succeeds(engine: Engine, monkeypatch: pytest.MonkeyPatch) -> None:
    transport = FakeTransport(failures=2)
    client = _real_client(engine, transport, monkeypatch)
    result = client.complete(model=HAIKU, system="s", messages=MESSAGES, max_tokens=10, purpose="x")
    assert result.text == "ok"
    assert len(transport.calls) == 3


def test_gives_up_after_max_attempts(engine: Engine, monkeypatch: pytest.MonkeyPatch) -> None:
    transport = FakeTransport(failures=99)
    client = _real_client(engine, transport, monkeypatch)
    with pytest.raises(ClaudeUnavailable):
        client.complete(model=HAIKU, system="s", messages=MESSAGES, max_tokens=10, purpose="x")
    assert len(transport.calls) == 4


def test_cost_estimates_use_deck_prices() -> None:
    assert estimate_cost(HAIKU, Usage(input_tokens=1_000_000, output_tokens=1_000_000)) == 0.60
    assert estimate_cost(SONNET, Usage(input_tokens=1_000_000, output_tokens=1_000_000)) == 12.0
    assert estimate_cost("claude-opus-5-5", Usage(input_tokens=1_000_000)) == 4.0
    cached = estimate_cost(SONNET, Usage(cache_read_tokens=1_000_000))
    assert cached == pytest.approx(0.2)


def test_spend_cap_blocks_calls(engine: Engine, monkeypatch: pytest.MonkeyPatch) -> None:
    # Cap is $1.00 in tests. Each Sonnet call of 50k in + 100k out costs $1.10.
    transport = FakeTransport(usage=Usage(input_tokens=50_000, output_tokens=100_000))
    client = _real_client(engine, transport, monkeypatch)
    client.complete(
        model=SONNET, system="s", messages=MESSAGES, max_tokens=10, purpose="x", business_id=None
    )
    with pytest.raises(SpendCapReached) as caught:
        client.complete(model=SONNET, system="s", messages=MESSAGES, max_tokens=10, purpose="x")
    assert "spend cap" in str(caught.value)
    assert caught.value.key == "error.spend_cap"
    assert len(transport.calls) == 1


def test_spend_cap_resets_on_new_ist_day(engine: Engine, monkeypatch: pytest.MonkeyPatch) -> None:
    transport = FakeTransport(usage=Usage(input_tokens=50_000, output_tokens=100_000))
    # 23:50 IST on 14 Nov, then 00:10 IST on 15 Nov (still 14 Nov in UTC).
    late = datetime(2026, 11, 14, 23, 50, tzinfo=IST)
    client = _real_client(engine, transport, monkeypatch, clock=late)
    client.complete(model=SONNET, system="s", messages=MESSAGES, max_tokens=10, purpose="x")
    next_day = datetime(2026, 11, 15, 0, 10, tzinfo=IST)
    client2 = _real_client(engine, transport, monkeypatch, clock=next_day)
    client2.complete(model=SONNET, system="s", messages=MESSAGES, max_tokens=10, purpose="x")
    assert len(transport.calls) == 2


def test_today_ist_crosses_midnight_before_utc() -> None:
    from datetime import UTC

    utc_evening = datetime(2026, 11, 14, 19, 0, tzinfo=UTC)  # 00:30 IST on 15 Nov
    assert str(today_ist(utc_evening)) == "2026-11-15"


def test_mock_calls_do_not_count_toward_cap(engine: Engine) -> None:
    client = ClaudeClient(get_settings(), engine)
    for _ in range(3):
        client.complete(
            model=SONNET,
            system="s",
            messages=MESSAGES,
            max_tokens=10,
            purpose="x",
            fixture_key="example",
        )
    assert client.spent_today() == 0.0
