"""When today's Claude spend cap is reached, judges can still try the sample voice notes and
open the demo businesses; only new Claude calls get the friendly message."""

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.engine import Engine
from sqlmodel import Session

from app.config import get_settings
from app.models import ClaudeUsage
from app.services.claude_client import today_ist
from tests.test_intake_flow import TEXT, _start


@pytest.fixture
def cap_reached(monkeypatch: pytest.MonkeyPatch, engine: Engine) -> Iterator[None]:
    monkeypatch.setenv("CLAUDE_MOCK", "0")  # real mode: the cap is checked before any call
    monkeypatch.setenv("CLAUDE_DAILY_SPEND_CAP_USD", "4")
    get_settings.cache_clear()
    with Session(engine) as session:
        session.add(
            ClaudeUsage(
                purpose="extract",
                model="claude-sonnet-5-5",
                cost_usd_estimate=4.5,
                is_mock=False,
                day_ist=today_ist(),
            )
        )
        session.commit()
    yield
    get_settings.cache_clear()


@pytest.mark.parametrize("sample", ["sample-en", "sample-hi", "sample-mr"])
def test_samples_still_work_at_the_cap(
    client: TestClient, engine: Engine, cap_reached: None, sample: str
) -> None:
    token = _start(client, engine, sample=sample)
    page = client.get(f"/start/{token}/confirm")
    assert page.status_code == 200 and "Kothrud" in page.text  # her details, ready to confirm


def test_new_claude_calls_get_the_friendly_message(
    client: TestClient, engine: Engine, cap_reached: None
) -> None:
    token = _start(client, engine, source="text", text=TEXT)
    page = client.get(f"/start/{token}/wait", follow_redirects=True).text
    assert "Our helper is resting for today" in page


def test_demo_businesses_still_open_at_the_cap(
    client: TestClient, engine: Engine, cap_reached: None
) -> None:
    from tests.test_publish import _business

    business_id, _, _ = _business(engine, slug="demo-bakery", is_sample=True, source="seed")
    from app.services import publish as pub

    pub.build_draft(engine, get_settings(), business_id)
    pub.publish(engine, get_settings(), business_id)
    site = client.get("/", headers={"Host": "demo-bakery.localhost:8000"})
    assert site.status_code == 200 and "btn-main" in site.text
    assert client.get("/").status_code == 200
