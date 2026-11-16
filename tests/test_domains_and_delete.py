"""GoDaddy web address suggestions, and "Delete my site and data"."""

import asyncio
import re
import shutil
import subprocess
from datetime import timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.engine import Engine
from sqlmodel import Session, select

from app.config import get_settings
from app.models import (
    ActionLog,
    Business,
    DomainLookup,
    DomainRequest,
    Enquiry,
    IntakeSession,
    Job,
    Photo,
    ProfileVersion,
    SiteVersion,
    TestRun,
    utcnow,
)
from app.services import godaddy
from app.services import publish as pub
from app.services.godaddy import (
    DOMAINS_JOB,
    Check,
    GddyCli,
    command_for,
    domains_runner,
    parse_check,
)
from app.services.jobs import JobWorker
from app.services.mail_jobs import EMAIL_JOB, team_email
from app.services.security import CSRF_FIELD
from tests.test_publish import _business

REAL_OUTPUT = """{"data": {"available": true, "currency": "USD", "definitive": true,
 "domain": "ashahomebakery.in", "terms": [{"period": 1, "price": "12.99"},
 {"period": 2, "price": "25.98"}]}}"""


def _csrf(client: TestClient, path: str) -> str:
    return re.search(rf'name="{CSRF_FIELD}" value="([^"]+)"', client.get(path).text).group(1)  # type: ignore[union-attr]


def _signed_in(
    client: TestClient, engine: Engine, slug: str = "asha-home-bakery"
) -> tuple[int, str]:
    business_id, token, pin = _business(engine, slug=slug)
    link = f"/o/{token}"
    client.post(link, data={CSRF_FIELD: _csrf(client, link), "pin": pin})
    return business_id, pin


def _run_lookups(engine: Engine) -> None:
    asyncio.run(
        JobWorker(engine, {DOMAINS_JOB: domains_runner(engine, get_settings())}).run_until_idle()
    )


# --- the GoDaddy CLI ---


def test_reads_the_real_cli_output() -> None:
    check = parse_check("ashahomebakery.in", REAL_OUTPUT, command_for("ashahomebakery.in"))
    assert check.available and check.price_usd == 12.99 and check.currency == "USD"


def test_cli_command_is_a_fixed_argument_list() -> None:
    assert command_for("ashahomebakery.in") == (
        "gddy",
        "domain",
        "available",
        "ashahomebakery.in",
        "--check-type",
        "full",
        "--json",
    )
    for bad in ("evil.in; rm -rf /", "-x.in", "a.in", "abc.org", "ABC.in", "abc .in"):
        with pytest.raises(ValueError, match="domain"):
            command_for(bad)


def test_cli_runs_without_a_shell_and_with_the_pat(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict[str, Any]] = []

    class Done:
        returncode, stdout = 0, REAL_OUTPUT

    def fake_run(args: list[str], **kwargs: Any) -> Done:
        calls.append({"args": args, **kwargs})
        return Done()

    monkeypatch.setenv("GDDY_PAT", "test-pat-value")
    get_settings.cache_clear()
    monkeypatch.setattr(subprocess, "run", fake_run)
    cli = GddyCli(get_settings(), binary="/usr/bin/gddy")
    assert cli.check("ashahomebakery.in").price_usd == 12.99
    call = calls[0]
    assert call["args"][0] == "/usr/bin/gddy" and "shell" not in call
    assert call["env"]["GDDY_PAT"] == "test-pat-value" and call["timeout"] == 20


@pytest.mark.parametrize("problem", ["missing", "timeout", "exit", "garbage"])
def test_cli_failures_never_raise(monkeypatch: pytest.MonkeyPatch, problem: str) -> None:
    def fake_run(*_a: Any, **_k: Any) -> Any:
        if problem == "timeout":
            raise subprocess.TimeoutExpired("gddy", 20)

        class Done:
            returncode = 1 if problem == "exit" else 0
            stdout = "not json"

        return Done()

    monkeypatch.setattr(subprocess, "run", fake_run)
    cli = GddyCli(get_settings(), binary=None if problem == "missing" else "/usr/bin/gddy")
    if problem == "missing":
        cli.binary = None
    result = cli.check("ashahomebakery.in")
    assert result.error and not result.available


# --- her page ---


def test_suggestions_show_only_free_names_with_prices(client: TestClient, engine: Engine) -> None:
    business_id, _ = _signed_in(client, engine)
    page = client.get("/owner").text
    assert "Get your own web address" in page and "Show me web addresses" in page
    client.post("/owner/domains", data={CSRF_FIELD: _csrf(client, "/owner")})
    assert "Checking web addresses with GoDaddy" in client.get("/owner").text
    _run_lookups(engine)
    page = client.get("/owner").text
    assert "ashahomebakery.in" in page and "$12.99" in page and "about ₹1091" in page
    assert "price before tax" in page
    assert "ashabakery.in" not in page.split("How Toran found these")[0]  # taken: not offered
    assert "ashascakes.in" in page and ">ashascakes.com<" not in page
    assert "set this up for you and tell you when it" in page and "Nothing is bought" in page
    with Session(engine) as session:
        lookup = session.exec(select(DomainLookup)).one()
        calls = session.exec(select(ActionLog).where(ActionLog.run_id == f"dom_{lookup.id}")).all()
    assert lookup.business_id == business_id and lookup.is_mock
    tools = [c.tool for c in calls]
    assert (
        tools.count("claude.suggest_names") == 1 and tools.count("godaddy.domain_available") == 12
    )
    assert "gddy domain available ashahomebakery.in --check-type full --json" in page


def test_results_are_cached_for_an_hour(client: TestClient, engine: Engine) -> None:
    _signed_in(client, engine)
    csrf = _csrf(client, "/owner")
    client.post("/owner/domains", data={CSRF_FIELD: csrf})
    _run_lookups(engine)
    client.post("/owner/domains", data={CSRF_FIELD: csrf})  # within the hour: the same lookup
    with Session(engine) as session:
        assert len(session.exec(select(DomainLookup)).all()) == 1
        assert len(session.exec(select(Job).where(Job.kind == DOMAINS_JOB)).all()) == 1
        lookup = session.exec(select(DomainLookup)).one()
        lookup.created_at = utcnow() - timedelta(minutes=61)
        session.add(lookup)
        session.commit()
    assert "Show me web addresses" in client.get("/owner").text  # stale results are not shown
    client.post("/owner/domains", data={CSRF_FIELD: csrf})
    with Session(engine) as session:
        assert len(session.exec(select(DomainLookup)).all()) == 2


def test_the_routes_are_rate_limited(client: TestClient, engine: Engine) -> None:
    _signed_in(client, engine)
    csrf = _csrf(client, "/owner")
    locations = [
        client.post("/owner/domains", data={CSRF_FIELD: csrf}, follow_redirects=False).headers[
            "location"
        ]
        for _ in range(7)
    ]
    assert "domains_error=too_many" in locations[-1]


def test_missing_cli_hides_the_section(
    client: TestClient, engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CLAUDE_MOCK", "0")
    get_settings.cache_clear()
    monkeypatch.setattr(shutil, "which", lambda _name: None)
    _signed_in(client, engine)
    page = client.get("/owner").text
    assert "Web address suggestions are not available right now" in page
    assert "Show me web addresses" not in page


def test_failed_lookup_shows_a_friendly_note(client: TestClient, engine: Engine) -> None:
    class Broken:
        installed = True

        def check(self, domain: str) -> Check:
            return Check(domain, False, None, "USD", False, command_for(domain), "exit 1")

    business_id, _ = _signed_in(client, engine)
    lookup_id = godaddy.start_lookup(engine, business_id)
    godaddy.run_lookup(engine, get_settings(), lookup_id, checker=Broken())
    page = client.get("/owner").text
    assert "Web address suggestions are not available right now" in page
    assert client.get("/owner").status_code == 200


def test_i_want_this_one_records_and_emails_the_team(client: TestClient, engine: Engine) -> None:
    business_id, _ = _signed_in(client, engine)
    csrf = _csrf(client, "/owner")
    client.post("/owner/domains", data={CSRF_FIELD: csrf})
    _run_lookups(engine)
    client.post("/owner/domains/request", data={CSRF_FIELD: csrf, "domain": "ashahomebakery.in"})
    with Session(engine) as session:
        request = session.exec(select(DomainRequest)).one()
        job = session.exec(select(Job).where(Job.kind == EMAIL_JOB)).one()
    assert (request.business_id, request.domain, request.price_usd) == (
        business_id,
        "ashahomebakery.in",
        12.99,
    )
    assert job.payload == {"team": "domain_request", "request_id": request.id}
    message = team_email(engine, get_settings(), job.payload)
    assert message is not None and "ashahomebakery.in" in message[1] and "godaddy.com" in message[1]
    assert "Nothing was bought" in message[1]
    page = client.get("/owner").text
    assert "You asked for ashahomebakery.in" in page and "tell you when it" in page


def test_a_tampered_domain_is_refused(client: TestClient, engine: Engine) -> None:
    _signed_in(client, engine)
    csrf = _csrf(client, "/owner")
    client.post("/owner/domains", data={CSRF_FIELD: csrf})
    _run_lookups(engine)
    for domain in ("google.com", "ashabakery.in"):  # never offered / taken
        location = client.post(
            "/owner/domains/request",
            data={CSRF_FIELD: csrf, "domain": domain},
            follow_redirects=False,
        ).headers["location"]
        assert "domains_error=not_offered" in location
    with Session(engine) as session:
        assert session.exec(select(DomainRequest)).all() == []


@pytest.mark.parametrize(
    ("lang", "title", "label"),
    [
        ("en", "Get your own web address", "price before tax"),
        ("hi", "अपना वेब पता पाइए", "टैक्स से पहले का दाम"),
        ("mr", "तुमचा स्वतःचा वेब पत्ता मिळवा", "करापूर्वीचा दर"),
    ],
)
def test_web_addresses_in_three_languages(
    client: TestClient, engine: Engine, lang: str, title: str, label: str
) -> None:
    _signed_in(client, engine)
    client.post("/owner/domains", data={CSRF_FIELD: _csrf(client, "/owner")})
    _run_lookups(engine)
    page = client.get(f"/owner?lang={lang}").text
    assert title in page and label in page


# --- delete my site and data ---


def _full_business(engine: Engine, slug: str) -> int:
    business_id, _, _ = _business(engine, slug=slug)
    settings = get_settings()
    pub.build_draft(engine, settings, business_id)
    pub.publish(engine, settings, business_id)
    settings.runs_dir.mkdir(parents=True, exist_ok=True)
    shot = settings.runs_dir / f"{slug}.png"
    shot.write_bytes(b"png")
    photo_folder = settings.uploads_dir / "photos" / str(business_id)
    photo_folder.mkdir(parents=True, exist_ok=True)
    (photo_folder / "a.jpg").write_bytes(b"jpg")
    with Session(engine) as session:
        session.add(Enquiry(business_id=business_id, name="Kavita", message="Cake?"))
        session.add(
            TestRun(business_id=business_id, site_hash="h", url="u", screenshot_path=shot.name)
        )
        session.add(Photo(business_id=business_id, filename="a.jpg"))
        session.add(DomainLookup(business_id=business_id, status="ok"))
        session.commit()
    return business_id


def test_delete_needs_the_pin_again(client: TestClient, engine: Engine) -> None:
    business_id, pin = _signed_in(client, engine)
    csrf = _csrf(client, "/owner")
    with Session(engine) as session:  # her PIN was entered more than 5 minutes ago
        pass
    client.cookies.delete("vsa_session")
    client.get("/owner")  # signed in again from the remembered device: no recent PIN
    page = client.get("/owner/delete", follow_redirects=False)
    assert page.status_code == 303 and page.headers["location"] == "/owner/pin?next=delete"
    csrf = _csrf(client, "/owner")
    refused = client.post("/owner/delete", data={CSRF_FIELD: csrf})
    assert refused.status_code == 403
    wrong = client.post(
        "/owner/pin",
        data={CSRF_FIELD: csrf, "pin": "0000" if pin != "0000" else "1111", "next": "delete"},
    )
    assert wrong.status_code == 401
    with Session(engine) as session:
        assert session.get(Business, business_id) is not None  # nothing deleted
    ok = client.post(
        "/owner/pin", data={CSRF_FIELD: csrf, "pin": pin, "next": "delete"}, follow_redirects=False
    )
    assert ok.headers["location"] == "/owner/delete"
    confirm = client.get("/owner/delete").text
    assert "This is permanent and cannot be undone." in confirm
    assert confirm.index("cannot be undone") < confirm.index("What will be deleted")
    assert "voice notes" in confirm.split("What will be deleted")[0]
    assert "Yes, delete everything" in confirm


def test_full_deletion_and_other_businesses_untouched(client: TestClient, engine: Engine) -> None:
    other = _full_business(engine, "someone-else")
    business_id, _ = _signed_in(client, engine, slug="asha-home-bakery")
    settings = get_settings()
    pub.build_draft(engine, settings, business_id)
    pub.publish(engine, settings, business_id)
    with Session(engine) as session:
        session.add(Enquiry(business_id=business_id, name="Meera", message="Hi"))
        session.add(
            TestRun(business_id=business_id, site_hash="h", url="u", screenshot_path="mine.png")
        )
        session.add(
            DomainRequest(business_id=business_id, domain="ashahomebakery.in", price_usd=12.99)
        )
        session.commit()
    settings.runs_dir.mkdir(parents=True, exist_ok=True)
    (settings.runs_dir / "mine.png").write_bytes(b"png")
    site = {"Host": "asha-home-bakery.localhost:8000"}
    assert client.get("/", headers=site).status_code == 200
    page = client.post("/owner/delete", data={CSRF_FIELD: _csrf(client, "/owner")})
    assert page.status_code == 200 and "Your site and data are deleted" in page.text
    assert client.get("/", headers=site).status_code == 404  # offline at once
    assert client.get("/owner").status_code == 401  # signed out
    with Session(engine) as session:
        assert session.get(Business, business_id) is None
        for table in (
            ProfileVersion,
            SiteVersion,
            Enquiry,
            TestRun,
            IntakeSession,
            DomainRequest,
            DomainLookup,
            Photo,
        ):
            rows = session.exec(select(table).where(table.business_id == business_id)).all()
            assert rows == [], table.__name__
        assert session.get(Business, other) is not None
        assert session.exec(select(Enquiry).where(Enquiry.business_id == other)).all()
        assert session.exec(select(TestRun).where(TestRun.business_id == other)).all()
        assert session.exec(select(SiteVersion).where(SiteVersion.business_id == other)).all()
        team = [
            j
            for j in session.exec(select(Job).where(Job.kind == EMAIL_JOB)).all()
            if j.payload and j.payload.get("team") == "deletion"
        ]
    assert not (settings.sites_dir / "asha-home-bakery").exists()
    assert not (settings.runs_dir / "mine.png").exists()
    assert (settings.sites_dir / "someone-else" / "live" / "index.html").is_file()
    assert (settings.runs_dir / "someone-else.png").is_file()
    assert len(team) == 1
    message = team_email(engine, settings, team[0].payload or {})
    assert message is not None
    assert "Asha" not in message[0] + message[1] and "asha" not in message[1]


@pytest.mark.parametrize(
    ("lang", "goodbye"),
    [
        ("en", "Your site and data are deleted"),
        ("hi", "आपकी साइट और डेटा हटा दिए गए"),
        ("mr", "तुमची साइट आणि डेटा काढून टाकला"),
    ],
)
def test_goodbye_in_three_languages(
    client: TestClient, engine: Engine, lang: str, goodbye: str
) -> None:
    _signed_in(client, engine)
    client.get(f"/owner?lang={lang}")  # she switched language on her page earlier
    page = client.post("/owner/delete", data={CSRF_FIELD: _csrf(client, "/owner")})
    assert goodbye in page.text


def test_delete_link_on_her_page(client: TestClient, engine: Engine) -> None:
    _signed_in(client, engine)
    assert 'href="/owner/delete"' in client.get("/owner").text
