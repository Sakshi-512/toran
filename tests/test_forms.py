"""Form quality: the home-page contact form (live checks, errors next to the field, typed text kept)
and the enquiry form on business sites (native attributes only, no JavaScript, named errors)."""

import re
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from markupsafe import escape
from playwright.sync_api import Browser, sync_playwright
from sqlalchemy.engine import Engine
from sqlmodel import Session, select

from app.models import Business, Enquiry
from app.services import field_rules
from app.services.i18n import t
from app.services.security import CSRF_FIELD
from tests.live_server import live_server
from tests.site_helpers import build_sample, page
from tests.test_add_rows import _in_thread
from tests.test_mail import TEAM, _drain, _wait_for_outbox
from tests.test_menu import _sign_in

LANGS = ["en", "hi", "mr"]


def _e(key: str, lang: str = "en") -> str:
    return str(escape(t(key, lang)))


def _csrf(client: TestClient) -> str:
    match = re.search(rf'name="{CSRF_FIELD}" value="([^"]+)"', client.get("/").text)
    assert match
    return match.group(1)


def _contacts(engine: Engine) -> list[Enquiry]:
    with Session(engine) as session:
        return list(session.exec(select(Enquiry).where(Enquiry.business_id == None)))  # noqa: E711


# --- contact form, server side -------------------------------------------------------------------


@pytest.mark.parametrize(
    ("bad", "field", "key"),
    [
        ({"name": ""}, "name", "contact.name_invalid"),
        ({"name": "x" * 121}, "name", "contact.name_invalid"),
        ({"contact": "not-an-email"}, "contact", "contact.bad_contact"),
        ({"contact": "12345"}, "contact", "contact.bad_contact"),
        ({"message": "x" * 2001}, "message", "contact.message_invalid"),
    ],
)
def test_contact_error_shows_next_to_the_field_and_keeps_the_text(
    client: TestClient, engine: Engine, bad: dict[str, str], field: str, key: str
) -> None:
    form = {"name": "Kavita", "contact": "kavita@example.test", "message": "Hello Toran!", **bad}
    response = client.post("/contact?lang=hi", data={CSRF_FIELD: _csrf(client), **form})
    assert response.status_code == 422
    html = response.text
    assert f'<p class="field-error" role="alert" id="c-{field}-error">{_e(key, "hi")}</p>' in html
    assert f'aria-describedby="c-{field}-error"' in html
    if field != "message":
        assert f'value="{escape(form[field])}"' in html
    assert 'action="/contact#contact"' in html
    assert _contacts(engine) == []


# --- enquiry form on business sites --------------------------------------------------------------


def test_enquiry_form_has_native_limits_and_no_script() -> None:
    site, _ = build_sample("sunita-beauty-parlour")
    for path in ("index.html", "hi/index.html", "mr/index.html"):
        html = site.files[path].decode("utf-8")
        assert "<script" not in html.lower(), path
        form = html.split("<!--enquiry-form-->")[1].split("<!--/enquiry-form-->")[0]
        assert f'name="name" required maxlength="{field_rules.ENQUIRY_MAX_NAME}"' in form
        assert 'autocomplete="name" enterkeyhint="next"' in form
        assert f'type="tel" maxlength="{field_rules.ENQUIRY_MAX_PHONE}"' in form
        assert 'autocomplete="tel" inputmode="tel"' in form
        pattern = str(escape(field_rules.ENQUIRY_PHONE_PATTERN))
        assert f'pattern="{pattern}"' in form and 'title="' in form
        assert f'name="message" required maxlength="{field_rules.ENQUIRY_MAX_MESSAGE}"' in form
        assert 'id="enq-phone-hint"' in form and 'id="enq-message-hint"' in form
    assert page(site).text.count("enquiry-hint") == 0  # hints are markup, not visible text


@pytest.fixture(scope="module")
def browser() -> Iterator[Browser]:
    with sync_playwright() as p:
        launched = p.chromium.launch()
        yield launched
        launched.close()


def test_enquiry_phone_pattern_works_in_a_browser(browser: Browser) -> None:
    site, _ = build_sample("sunita-beauty-parlour")
    html = site.files["index.html"].decode("utf-8")
    pg = browser.new_page()
    pg.set_content(html)
    check = (
        "(v) => { const f = document.getElementById('enq-phone');"
        " f.value = v; return f.validity.valid; }"
    )
    for good in ("98765 43210", "+91 98765-43210", "(020) 2345.6789", ""):
        assert pg.evaluate(check, good), good
    for bad in ("98a65", "९८७६५४३२१०", "call me", "1" * 21):
        assert not pg.evaluate(check, bad), bad
    pg.close()


def test_enquiry_reaches_the_owner_inbox_and_the_team_mailbox(
    client: TestClient, engine: Engine
) -> None:
    business_id, _ = _sign_in(client, engine)
    with Session(engine) as session:
        slug = session.get(Business, business_id).slug  # type: ignore[union-attr]
    origin = f"https://{slug}.example.test"
    form = {"name": "Kavita", "phone": "90000 12345", "message": "Is Sunday free?", "lang": "mr"}
    sent = client.post(
        f"/api/enquiry/{slug}", data=form, headers={"Origin": origin}, follow_redirects=False
    )
    assert sent.status_code == 303 and sent.headers["location"] == f"{origin}/mr/thanks/"
    assert "Is Sunday free?" in client.get("/owner").text  # the owner's enquiries inbox
    _in_thread(lambda: _drain(engine))
    assert any(
        m["To"] == TEAM and "Is Sunday free?" in m.get_content() for m in _wait_for_outbox(1)
    )
    # An invalid phone is refused, and the message names the field in the site's language.
    bad = client.post(
        f"/api/enquiry/{slug}",
        data={**form, "phone": "call me"},
        headers={"Origin": origin},
        follow_redirects=False,
    )
    assert bad.status_code == 422 and _e("enquiry.phone_invalid", "mr") in bad.text


# --- contact form in a real browser --------------------------------------------------------------


def _msg(pg: object, field: str) -> str:
    return str(
        pg.evaluate(  # type: ignore[attr-defined]
            """(id) => { const m = document.getElementById(id + '-live-msg');
               return m && !m.hidden ? m.textContent : ''; }""",
            field,
        )
    )


@pytest.mark.parametrize("lang", LANGS)
def test_contact_form_live_checks(browser: Browser, engine: Engine, lang: str) -> None:
    problems: list[str] = []
    with live_server() as port:
        pg = browser.new_page()
        for width in (360, 1280):
            where = f"{lang} {width}px"
            pg.set_viewport_size({"width": width, "height": 900})
            pg.goto(f"http://localhost:{port}/?lang={lang}")
            pg.fill("#c-contact", "kavita@example")
            pg.evaluate("document.activeElement.blur()")
            pg.focus("#c-contact")
            pg.evaluate("document.activeElement.blur()")
            if _msg(pg, "c-contact") != t("contact.bad_contact", lang):
                problems.append(f"{where}: bad email not flagged")
            pg.fill("#c-contact", "98765 43210")
            if _msg(pg, "c-contact"):
                problems.append(f"{where}: a phone number was flagged")
            long_name = "N" * 200
            pg.fill("#c-name", "")
            pg.focus("#c-name")
            pg.keyboard.insert_text(long_name)
            pg.evaluate("document.activeElement.blur()")
            if pg.input_value("#c-name") != long_name:
                problems.append(f"{where}: pasted name was cut")
            if t("v.too_long", lang).split("{")[0].strip()[:8] not in _msg(pg, "c-name"):
                problems.append(f"{where}: 200-character name not flagged: {_msg(pg, 'c-name')!r}")
            pg.fill("#c-message", "m" * 1700)
            count = pg.evaluate(
                "(() => { const m = document.getElementById('c-message-field-count');"
                " return m && !m.hidden ? m.textContent : ''; })()"
            )
            if count != t("v.counter", lang).format(n=1700, max=2000):
                problems.append(f"{where}: no counter near the limit: {count!r}")
            pg.click("#contact button[type=submit]")
            pg.wait_for_selector("#v-summary")
            if pg.evaluate("document.activeElement.id") != "c-name":
                problems.append(f"{where}: focus not on the first invalid field")
            if pg.is_disabled("#contact button[type=submit]"):
                problems.append(f"{where}: send button stuck disabled after a blocked send")
            if pg.evaluate("document.documentElement.scrollWidth > innerWidth"):
                problems.append(f"{where}: sideways scroll")
        pg.close()
    assert not problems, "\n".join(problems)
    assert _in_thread(lambda: _contacts(engine)) == []


def test_contact_without_javascript_shows_the_server_error(
    browser: Browser, engine: Engine
) -> None:
    with live_server() as port:
        context = browser.new_context(java_script_enabled=False)
        pg = context.new_page()
        pg.goto(f"http://localhost:{port}/?lang=mr")
        pg.fill("#c-name", "Kavita")
        pg.fill("#c-contact", "not-an-email")
        pg.fill("#c-message", "Hello")
        with pg.expect_navigation():
            pg.click("#contact button[type=submit]")
        assert pg.url.endswith("/contact#contact")
        assert t("contact.bad_contact", "mr") in pg.inner_text("#c-contact-error")
        assert (
            pg.input_value("#c-contact") == "not-an-email" and pg.input_value("#c-name") == "Kavita"
        )
        context.close()


def test_contact_double_tap_sends_once(browser: Browser, engine: Engine) -> None:
    with live_server() as port:
        pg = browser.new_page()
        pg.goto(f"http://localhost:{port}/")
        pg.fill("#c-name", "Kavita")
        pg.fill("#c-contact", "kavita@example.test")
        pg.fill("#c-message", "Hello Toran!")
        with pg.expect_navigation():
            pg.dblclick("#contact button[type=submit]")
        pg.wait_for_load_state("load")
        assert t("contact.thanks", "en") in pg.inner_text("#contact")
        pg.close()
    assert len(_in_thread(lambda: _contacts(engine))) == 1
