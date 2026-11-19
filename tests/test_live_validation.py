"""Live field checks in a real browser (app/static/validate.js): a progressive enhancement over
the server's checks, on the confirm page, My details and the menu editor."""

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from playwright.sync_api import Browser, Page, sync_playwright
from sqlalchemy.engine import Engine
from sqlmodel import Session, select

from app.models import Business
from app.services.i18n import t
from tests.live_server import live_server
from tests.test_add_rows import _in_thread
from tests.test_intake_flow import TEXT, _start
from tests.test_menu import _finish_jobs
from tests.test_publish import _business

LANGS = ["en", "hi", "mr"]
WIDTHS = (360, 1280)
GIBBERISH = "klmeds.,c≤.,/z.c,/scsc"


@pytest.fixture(scope="module")
def browser() -> Iterator[Browser]:
    with sync_playwright() as p:
        launched = p.chromium.launch()
        yield launched
        launched.close()


def _msg(page: Page, field: str) -> str:
    """The visible live message under a field ("" if none)."""
    return str(
        page.evaluate(
            """(name) => { const f = document.querySelector(`[name="${name}"]`);
               const m = document.getElementById(f.id + '-live-msg');
               return m && !m.hidden ? m.textContent : ''; }""",
            field,
        )
    )


def _count(page: Page, field: str) -> str:
    return str(
        page.evaluate(
            """(name) => { const f = document.querySelector(`[name="${name}"]`);
               const m = document.getElementById(f.id + '-field-count');
               return m && !m.hidden ? m.textContent : ''; }""",
            field,
        )
    )


def _set(page: Page, selector: str, text: str) -> None:
    """Clear a field and type into it as a person would (or paste, for long text)."""
    page.fill(selector, "")
    page.focus(selector)
    page.keyboard.insert_text(text)


def _blur(page: Page) -> None:
    page.evaluate("document.activeElement.blur()")


def _confirm_url(client: TestClient, engine: Engine) -> str:
    token: str = _in_thread(lambda: _start(client, engine, source="text", text=TEXT))
    return token


@pytest.mark.parametrize("lang", LANGS)
def test_fields_check_live_and_never_drop_text(
    browser: Browser, client: TestClient, engine: Engine, lang: str
) -> None:
    token = _confirm_url(client, engine)
    problems: list[str] = []
    with live_server() as port:
        page = browser.new_page()
        for width in WIDTHS:
            where = f"{lang} {width}px"
            page.set_viewport_size({"width": width, "height": 900})
            page.goto(f"http://localhost:{port}/start/{token}/confirm?lang={lang}")
            # Phone: letters are not taken; Latin and Devanagari digits are.
            page.fill("#f-phone", "")
            page.focus("#f-phone")
            page.keyboard.type("98a76b")
            if page.input_value("#f-phone") != "9876":
                problems.append(f"{where}: phone kept letters: {page.input_value('#f-phone')}")
            if _msg(page, "phone") != t("v.phone_letters", lang):
                problems.append(f"{where}: no message for a blocked letter")
            _set(page, "#f-phone", "९८७६५४३२१०")
            _blur(page)
            if page.input_value("#f-phone") != "९८७६५४३२१०" or _msg(page, "phone"):
                problems.append(f"{where}: Devanagari phone refused: {_msg(page, 'phone')}")
            _set(page, "#f-phone", "12345")
            if _msg(page, "phone") != t("intake.phone_invalid", lang):
                problems.append(f"{where}: bad phone not flagged")
            # Business name: gibberish, too long (kept whole), fine names, counter near the limit.
            _set(page, "#f-name", GIBBERISH)
            _blur(page)
            if _msg(page, "business_name") != t("intake.name_chars", lang):
                problems.append(f"{where}: gibberish not flagged: {_msg(page, 'business_name')!r}")
            if page.get_attribute("#f-name", "aria-invalid") != "true":
                problems.append(f"{where}: no aria-invalid")
            if "f-name-live-msg" not in (page.get_attribute("#f-name", "aria-describedby") or ""):
                problems.append(f"{where}: message not linked with aria-describedby")
            long_name = "A" * 121
            _set(page, "#f-name", long_name)
            if page.input_value("#f-name") != long_name:
                problems.append(
                    f"{where}: pasted name was cut to {len(page.input_value('#f-name'))}"
                )
            if _msg(page, "business_name") != t("intake.name_length", lang):
                problems.append(f"{where}: 121 characters not flagged")
            for fine in ("Kashvi Beauty Parlour", "आशा होम बेकरी"):
                _set(page, "#f-name", fine)
                if _msg(page, "business_name") or page.get_attribute("#f-name", "aria-invalid"):
                    problems.append(f"{where}: {fine} flagged: {_msg(page, 'business_name')}")
            _set(page, "#f-name", "B" * 100)
            if _count(page, "business_name") != t("v.counter", lang).format(n=100, max=120):
                problems.append(
                    f"{where}: no counter at 100 of 120: {_count(page, 'business_name')!r}"
                )
            _set(page, "#f-name", "B" * 50)
            if _count(page, "business_name"):
                problems.append(f"{where}: counter shown far from the limit")
            # Web address: shown as a message, never rewritten.
            _set(page, "#f-address", "My Bakery!")
            _blur(page)
            if page.input_value("#f-address") != "My Bakery!":
                problems.append(f"{where}: address was rewritten")
            if _msg(page, "web_address") != t("intake.address_invalid", lang):
                problems.append(f"{where}: bad address not flagged")
            # Prices: Devanagari digits are fine, letters are not.
            _set(page, 'input[name="p0_o0_price"]', "४५०")
            _blur(page)
            if _msg(page, "p0_o0_price"):
                problems.append(f"{where}: Devanagari price refused")
            _set(page, 'input[name="p0_o0_price"]', "45O")
            if _msg(page, "p0_o0_price") != t("intake.price_invalid", lang):
                problems.append(f"{where}: price with a letter not flagged")
            # Text fields: counter near the limit, message over it, no filtering.
            _set(page, "#f-lead", "x" * 120)
            if _count(page, "lead_time") != t("v.at_limit", lang).format(max=120):
                problems.append(f"{where}: no message at the lead time limit")
        page.close()
    assert not problems, "\n".join(problems)


@pytest.mark.parametrize("lang", LANGS)
def test_confirm_with_a_problem_does_not_submit_and_save_still_does(
    browser: Browser, client: TestClient, engine: Engine, lang: str
) -> None:
    token = _confirm_url(client, engine)
    problems: list[str] = []
    with live_server() as port:
        page = browser.new_page()
        url = f"http://localhost:{port}/start/{token}/confirm?lang={lang}"
        for width in WIDTHS:
            where = f"{lang} {width}px"
            page.set_viewport_size({"width": width, "height": 900})
            page.goto(url)
            _set(page, "#f-name", GIBBERISH)
            page.click('button[value="confirm"]')
            page.wait_for_selector("#v-summary")
            if page.url != url:
                problems.append(f"{where}: Confirm submitted ({page.url})")
            summary = page.inner_text("#v-summary")
            if t("v.summary_one", lang) not in summary:
                problems.append(f"{where}: summary says {summary!r}")
            if page.evaluate("document.activeElement.id") != "f-name":
                problems.append(f"{where}: focus is not on the invalid field")
            if page.is_disabled('button[value="confirm"]'):
                problems.append(f"{where}: Confirm stayed disabled after a blocked tap")
            page.click("#v-summary a")
            if page.evaluate("document.activeElement.id") != "f-name":
                problems.append(f"{where}: summary link does not focus the field")
            # Save changes is never blocked: it posts, and the server keeps what was typed.
            with page.expect_navigation():
                page.click('button[value="save"]')
            if page.input_value("#f-name") != GIBBERISH:
                problems.append(f"{where}: Save changes did not post")
        page.close()
    assert not problems, "\n".join(problems)


def test_keyboard_only_confirm(browser: Browser, client: TestClient, engine: Engine) -> None:
    token = _confirm_url(client, engine)
    with live_server() as port:
        page = browser.new_page()
        page.goto(f"http://localhost:{port}/start/{token}/confirm")
        page.focus("#f-name")
        page.keyboard.press("Control+A")
        page.keyboard.type("@@")
        page.keyboard.press("Tab")
        assert _msg(page, "business_name") == t("intake.name_chars", "en")
        page.focus('button[value="confirm"]')
        page.keyboard.press("Enter")
        page.wait_for_selector("#v-summary")
        assert page.evaluate("document.activeElement.id") == "f-name"
        page.close()


def test_without_javascript_the_server_checks_as_before(
    browser: Browser, client: TestClient, engine: Engine
) -> None:
    token = _confirm_url(client, engine)
    with live_server() as port:
        context = browser.new_context(java_script_enabled=False)
        page = context.new_page()
        page.goto(f"http://localhost:{port}/start/{token}/confirm")
        assert page.get_attribute("#f-name", "maxlength") == "120"  # the HTML limit stays
        page.fill("#f-name", GIBBERISH)
        with page.expect_navigation():
            page.click('button[value="confirm"]')
        assert t("intake.name_chars", "en") in page.inner_text("main")
        assert page.input_value("#f-name") == GIBBERISH
        context.close()


def test_back_link_asks_only_after_an_edit(
    browser: Browser, client: TestClient, engine: Engine
) -> None:
    token = _confirm_url(client, engine)
    with live_server() as port:
        page = browser.new_page()
        url = f"http://localhost:{port}/start/{token}/confirm?lang=hi"
        dialogs: list[str] = []

        def answer(dialog: object) -> None:
            dialogs.append(dialog.message)  # type: ignore[attr-defined]
            dialog.dismiss()  # type: ignore[attr-defined]

        page.on("dialog", answer)
        page.goto(url)
        page.fill("#f-area", "Kothrud West")
        page.click(".back-link")
        assert dialogs == [t("v.leave", "hi")] and page.url == url  # stayed
        with page.expect_navigation():
            page.click('button[value="save"]')
        with page.expect_navigation():
            page.click(".back-link")  # saved: no question
        assert len(dialogs) == 1 and page.url.split("?")[0].endswith("/start")
        page.goto(url)
        with page.expect_navigation():
            page.click(".back-link")  # nothing edited: no question
        assert len(dialogs) == 1
        page.close()


def test_double_tap_on_confirm_creates_one_business(
    browser: Browser, client: TestClient, engine: Engine
) -> None:
    token = _confirm_url(client, engine)

    def count() -> int:
        with Session(engine) as session:
            return len(session.exec(select(Business)).all())

    before = _in_thread(count)
    with live_server() as port:
        page = browser.new_page()
        page.goto(f"http://localhost:{port}/start/{token}/confirm")
        page.fill("#f-phone", "9876543210")
        with page.expect_navigation():
            page.dblclick('button[value="confirm"]')
        page.wait_for_load_state("load")
        assert "secret-value" in page.content()
        page.close()
    assert _in_thread(count) == before + 1


@pytest.mark.parametrize("lang", LANGS)
def test_my_details_and_menu_still_save(browser: Browser, engine: Engine, lang: str) -> None:
    _, link, pin = _business(engine)
    problems: list[str] = []
    with live_server() as port:
        base = f"http://localhost:{port}"
        context = browser.new_context()
        page = context.new_page()
        page.goto(f"{base}/o/{link}")
        page.fill("#pin", pin)
        page.click("button[type=submit]")
        page.wait_for_url(f"{base}/owner")
        for width in WIDTHS:
            where = f"{lang} {width}px"
            page.set_viewport_size({"width": width, "height": 900})
            page.goto(f"{base}/owner/details?lang={lang}")
            page.fill("#d-phone", "")
            page.focus("#d-phone")
            page.keyboard.type("98765x43210")
            if page.input_value("#d-phone") != "9876543210":
                problems.append(f"{where}: details phone kept a letter")
            _set(page, "#d-area", "Kothrud")
            page.fill("#d-pin", pin)  # a new phone number needs the PIN again
            with page.expect_navigation():
                page.click("form[data-live] button[type=submit]")
            if t("details.saved", lang) not in page.inner_text("main"):
                problems.append(f"{where}: details did not save")
            _in_thread(lambda: _finish_jobs(engine))  # the save queued a customer test
            page.goto(f"{base}/owner/menu?lang={lang}")
            with page.expect_navigation():
                page.click('form[data-live] button[value="save"]')
            text = page.inner_text("main")
            if t("menu.same", lang) not in text and t("menu.saved", lang) not in text:
                problems.append(f"{where}: menu save did not go through")
            _in_thread(lambda: _finish_jobs(engine))
        context.close()
    assert not problems, "\n".join(problems)
