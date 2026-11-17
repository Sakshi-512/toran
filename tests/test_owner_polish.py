import re
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.engine import Engine
from sqlmodel import Session, select

from app.config import get_settings
from app.models import Enquiry, OwnerDevice, ScoreItem, TestRun, utcnow
from app.services import publish as pub
from app.services import ratelimit
from app.services.auth import DEVICE_COOKIE, require_recent_pin, session_epoch
from app.services.score_summary import owner_summary
from app.services.security import CSRF_FIELD
from tests.test_publish import _business


def _csrf(client: TestClient, path: str) -> str:
    return re.search(rf'name="{CSRF_FIELD}" value="([^"]+)"', client.get(path).text).group(1)  # type: ignore[union-attr]


def _replace_cookie(client: TestClient, name: str, value: str) -> None:
    client.cookies.delete(name)
    client.cookies.set(name, value, domain="testserver.local")


def _sign_in(client: TestClient, engine: Engine) -> tuple[int, str]:
    business_id, token, pin = _business(engine)
    response = client.post(
        f"/o/{token}",
        data={CSRF_FIELD: _csrf(client, f"/o/{token}"), "pin": pin},
        follow_redirects=False,
    )
    assert response.headers["location"] == "/owner"
    return business_id, response.headers["set-cookie"]


# --- rate limits ---


def test_normal_browsing_stays_well_under_the_owner_limit(
    client: TestClient, engine: Engine
) -> None:
    business_id, _ = _sign_in(client, engine)
    pub.build_draft(engine, get_settings(), business_id)
    counted = 0
    for _ in range(5):  # open styles with all three previews and their files, five times
        page = client.get("/owner/styles")
        counted += 1
        assert page.status_code == 200
        for theme in ("warm", "elegant", "simple"):
            html = client.get(f"/owner/style-preview/{theme}/").text
            counted += 1
            for ref in set(re.findall(r'(?:href|src)="(assets/[^"]+)"', html)):
                assert client.get(f"/owner/style-preview/{theme}/{ref}").status_code == 200
                counted += 1
    for _ in range(40):  # ~3 minutes of the owner page refreshing every 5 seconds
        assert client.get("/owner").status_code == 200
        counted += 1
    html = client.get("/owner/preview/").text
    for ref in set(re.findall(r'(?:href|src)="(assets/[^"]+)"', html)):
        assert client.get(f"/owner/preview/{ref}").status_code == 200
        counted += 1
    assert counted < 600 / 3  # well under the 600-per-minute owner limit


def test_waiting_page_refreshes_never_hit_the_public_limit(
    client: TestClient, engine: Engine
) -> None:
    from tests.test_intake_flow import TEXT, _start

    token = _start(client, engine, source="text", text=TEXT)
    for _ in range(100):  # 5 minutes of refreshing every 3 seconds
        assert client.get(f"/start/{token}/wait", follow_redirects=False).status_code == 303
    assert client.get("/").status_code == 200  # the public limit is untouched


def test_misused_owner_session_is_still_limited(
    client: TestClient, engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    _sign_in(client, engine)
    monkeypatch.setattr(ratelimit, "OWNER_LIMIT", "3/minute")
    from app.main import create_app

    with TestClient(create_app(), raise_server_exceptions=False, cookies=client.cookies) as again:
        codes = [again.get("/owner").status_code for _ in range(4)]
    assert codes[-1] == 429


def test_start_form_shows_countdown_on_same_page(client: TestClient) -> None:
    token = _csrf(client, "/start")
    codes = [
        client.post("/start", data={CSRF_FIELD: token, "source": "text"}).status_code
        for _ in range(6)
    ]
    page = client.post(
        "/start", data={CSRF_FIELD: token, "source": "text"}, headers={"Accept-Language": "mr"}
    )
    assert codes[-1] == 429 and page.status_code == 429
    assert "व्हॉइस नोट रेकॉर्ड करा" in page.text  # still the start page, not an error page
    assert "कृपया एक मिनिट थांबून पुन्हा प्रयत्न करा." in page.text
    seconds = re.search(r'data-seconds="(\d+)"', page.text)
    assert seconds and 1 <= int(seconds.group(1)) <= 60
    assert 'src="/static/countdown.js?v=' in page.text


def test_pin_and_contact_forms_show_countdown(client: TestClient, engine: Engine) -> None:
    _, token, _ = _business(engine)
    csrf = _csrf(client, f"/o/{token}")
    for _ in range(6):
        page = client.post(f"/o/{token}", data={CSRF_FIELD: csrf, "pin": "0000"})
    assert page.status_code == 429 and "Enter your PIN" in page.text and "data-seconds" in page.text
    csrf = _csrf(client, "/")
    for _ in range(6):
        page = client.post(
            "/contact",
            data={CSRF_FIELD: csrf, "name": "A", "contact": "a@example.test", "message": "hi"},
            follow_redirects=False,
        )
    assert (
        page.status_code == 429
        and "Contact us" in page.text
        and "You can try again in" in page.text
    )


def test_other_limits_show_countdown_and_try_again_here(client: TestClient) -> None:
    for _ in range(61):
        page = client.get("/privacy")
    assert page.status_code == 429 and page.headers["retry-after"].isdigit()
    assert 'href="/privacy"' in page.text and "Go to home page" not in page.text


# --- score wording ---


def _items(points: list[tuple[float, float, str]]) -> list[ScoreItem]:
    return [
        ScoreItem(
            run_id=1,
            part="speed",
            key=f"k{i}",
            source="measured",
            status=status,
            points=p,
            max_points=m,
            reason_en="",
            reason_hi="",
            reason_mr="",
        )
        for i, (p, m, status) in enumerate(points)
    ]


@pytest.mark.parametrize(
    ("total", "items", "en"),
    [
        (
            88,
            [(14, 14, "scored"), (3, 6, "scored"), (0, 4, "scored")],
            "Your site is ready for most customers. 2 things to improve.",
        ),
        (70, [(5, 6, "scored")], "Your site is nearly ready. 1 thing to improve."),
        (
            40,
            [(4, 4, "scored")],
            "Your site needs a few fixes before customers see it. Nothing to improve right now.",
        ),
    ],
)
def test_owner_sentences(total: int, items: list[tuple[float, float, str]], en: str) -> None:
    summary = owner_summary(
        TestRun(business_id=1, site_hash="x", url="u", total=total), _items(items)
    )
    assert summary.headline("en") == f"{total}/100"
    assert summary.sentence("en") == en
    assert summary.sentence("hi") and summary.sentence("mr")


def test_pending_checks_say_still_running() -> None:
    run = TestRun(business_id=1, site_hash="x", url="u", total=40, pending_points=50)
    summary = owner_summary(run, _items([(4, 4, "scored"), (0, 20, "pending")]))
    assert summary.headline("en") == "A few checks are still running…"
    assert summary.headline("mr") == "काही तपासण्या अजून सुरू आहेत…" and summary.sentence("en") == ""


# --- remembered device ---


def test_correct_pin_remembers_device_for_30_days(client: TestClient, engine: Engine) -> None:
    business_id, cookie = _sign_in(client, engine)
    device_cookie = (
        next(c for c in cookie.split(", ") if c.startswith(f"{DEVICE_COOKIE}="))
        if ", " in cookie
        else cookie
    )
    assert "Max-Age=2592000" in cookie and "HttpOnly" in cookie and "SameSite=lax" in cookie
    assert "Secure" not in device_cookie  # http://localhost in tests; Secure in production
    with Session(engine) as session:
        device = session.exec(select(OwnerDevice)).one()
    assert device.business_id == business_id
    assert (client.cookies.get(DEVICE_COOKIE) or "") not in device.token_hash
    # The session ends (new browser session), but the device is remembered.
    client.cookies.delete("vsa_session")
    assert client.get("/owner").status_code == 200


def test_device_cookie_is_secure_in_production(
    monkeypatch: pytest.MonkeyPatch, engine: Engine
) -> None:
    monkeypatch.setenv("APP_ENV", "production")
    get_settings.cache_clear()
    from app.main import create_app

    _, token, pin = _business(engine)
    with TestClient(create_app(), base_url="https://example.test") as client:
        response = client.post(
            f"/o/{token}",
            data={CSRF_FIELD: _csrf(client, f"/o/{token}"), "pin": pin},
            follow_redirects=False,
        )
    device = [c for c in response.headers.get_list("set-cookie") if c.startswith(DEVICE_COOKIE)]
    assert device and "Secure" in device[0] and "HttpOnly" in device[0]


def test_expired_revoked_and_tampered_devices_are_rejected(
    client: TestClient, engine: Engine
) -> None:
    _sign_in(client, engine)
    real = client.cookies.get(DEVICE_COOKIE)
    client.cookies.delete("vsa_session")
    _replace_cookie(client, DEVICE_COOKIE, "tampered-token")
    assert client.get("/owner").status_code == 401
    _replace_cookie(client, DEVICE_COOKIE, real or "")
    with Session(engine) as session:
        device = session.exec(select(OwnerDevice)).one()
        device.expires_at = utcnow() - timedelta(seconds=1)
        session.add(device)
        session.commit()
    assert client.get("/owner").status_code == 401


def test_sign_out_revokes_the_device(client: TestClient, engine: Engine) -> None:
    _sign_in(client, engine)
    real = client.cookies.get(DEVICE_COOKIE)
    client.post("/owner/logout", data={CSRF_FIELD: _csrf(client, "/owner")})
    with Session(engine) as session:
        assert session.exec(select(OwnerDevice)).one().revoked_at is not None
    _replace_cookie(client, DEVICE_COOKIE, real or "")
    assert client.get("/owner").status_code == 401


def test_remembered_device_is_not_a_recent_pin(client: TestClient, engine: Engine) -> None:
    from starlette.requests import Request

    business_id, _ = _sign_in(client, engine)
    request = Request(
        {
            "type": "http",
            "session": {
                "owner_business_id": business_id,
                "owner_signed_in_at": utcnow().timestamp(),
                "owner_epoch": session_epoch(business_id),
            },
            "headers": [],
            "query_string": b"",
            "path": "/",
        }
    )
    with pytest.raises(Exception, match=r"owner\.pin_again"):
        require_recent_pin(request)


# --- enquiries inbox ---


def test_enquiries_inbox_newest_first_and_only_hers(client: TestClient, engine: Engine) -> None:
    business_id, _ = _sign_in(client, engine)
    other = _business(engine, slug="someone-else")[0]
    with Session(engine) as session:
        session.add(
            Enquiry(
                business_id=business_id,
                name="Kavita",
                phone="+919822012345",
                message="Is Sunday free?",
                created_at=utcnow() - timedelta(days=1),
            )
        )
        session.add(Enquiry(business_id=business_id, name="Meera", message="Eggless cake?"))
        session.add(Enquiry(business_id=other, name="Secret", message="Not hers"))
        session.commit()
    page = client.get("/owner").text
    assert page.index("Meera") < page.index("Kavita")
    assert 'href="tel:+919822012345"' in page and "Is Sunday free?" in page
    assert "Secret" not in page


def test_empty_inbox_message_in_her_language(client: TestClient, engine: Engine) -> None:
    _sign_in(client, engine)
    assert "अजून चौकश्या नाहीत" in client.get("/owner?lang=mr").text
