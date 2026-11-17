"""Confirm only once: the button is disabled after one tap, and a second submission gets a
clear page (the PIN was shown once) instead of an error. And the team's new-access command."""

import re
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.engine import Engine
from sqlmodel import Session, select

from app.models import ActionLog, Business, OwnerDevice
from app.services.auth import reissue_access
from app.services.security import CSRF_FIELD
from tests.test_intake_flow import TEXT, _cards, _confirm, _start
from tests.test_publish import _business


def _ready(client: TestClient, engine: Engine) -> tuple[str, dict[str, Any]]:
    token = _start(client, engine, source="text", text=TEXT)
    _, fields = _cards(client, token)
    fields.update(phone="9822012345", web_address="priya-cakes")
    return token, fields


def test_confirm_button_is_once_only(client: TestClient, engine: Engine) -> None:
    token, _ = _ready(client, engine)
    page = client.get(f"/start/{token}/confirm").text
    assert re.search(r'value="confirm" data-once="Creating your website…"', page)
    assert 'src="/static/once.js?v=' in page


def test_double_tap_shows_the_pin_was_shown_once(client: TestClient, engine: Engine) -> None:
    token, fields = _ready(client, engine)
    first = _confirm(client, token, fields)
    assert first.status_code == 200 and re.search(r'class="secret-value pin">\d{4}<', first.text)
    second = _confirm(client, token, fields)
    assert second.status_code == 409
    assert "Your website is already created" in second.text
    assert "shown only once" in second.text and "new link and PIN" in second.text
    assert not re.search(r'class="secret-value pin">\d{4}<', second.text)
    assert second.headers["cache-control"] == "no-store"
    with Session(engine) as session:
        assert len(session.exec(select(Business)).all()) == 1


@pytest.mark.parametrize(
    ("lang", "title"),
    [("hi", "आपकी वेबसाइट पहले ही बन चुकी है"), ("mr", "तुमची वेबसाइट आधीच तयार झाली आहे")],
)
def test_already_created_in_her_language(
    client: TestClient, engine: Engine, lang: str, title: str
) -> None:
    token, fields = _ready(client, engine)
    _confirm(client, token, fields)
    client.get(f"/start?lang={lang}")
    assert title in _confirm(client, token, fields).text


def test_new_access_stops_the_old_link_sessions_and_devices(
    client: TestClient, engine: Engine, caplog: pytest.LogCaptureFixture
) -> None:
    business_id, token, pin = _business(engine)
    link = f"/o/{token}"
    csrf = re.search(rf'name="{CSRF_FIELD}" value="([^"]+)"', client.get(link).text).group(1)  # type: ignore[union-attr]
    client.post(link, data={CSRF_FIELD: csrf, "pin": pin})
    assert client.get("/owner").status_code == 200
    access = reissue_access(engine, business_id)
    assert client.get("/owner").status_code == 401  # session and remembered device both end
    assert client.get(link).status_code == 404  # the old link no longer works
    with Session(engine) as session:
        assert all(d.revoked_at for d in session.exec(select(OwnerDevice)).all())
        logged = session.exec(select(ActionLog).where(ActionLog.step == "access_reissued")).one()
    assert logged.detail is None and logged.business_id == business_id
    assert access.link_token not in caplog.text and access.pin not in caplog.text
    new = f"/o/{access.link_token}"
    csrf = re.search(rf'name="{CSRF_FIELD}" value="([^"]+)"', client.get(new).text).group(1)  # type: ignore[union-attr]
    client.post(new, data={CSRF_FIELD: csrf, "pin": access.pin})
    assert client.get("/owner").status_code == 200
