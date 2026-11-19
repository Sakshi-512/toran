"""Loading and progress states: one waiting box (ring, plain words, real stages only), busy
buttons that never block a second legitimate tap after coming back, and forms that post exactly
as before with JavaScript off."""

import re
from collections.abc import Callable, Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient
from markupsafe import escape
from playwright.sync_api import Browser, Page, sync_playwright
from sqlalchemy.engine import Engine
from sqlmodel import Session

from app.config import get_settings
from app.models import AgentRun, Business, DomainLookup, Job
from app.services import publish as pub
from app.services.i18n import t
from app.services.score_runs import SCORE_JOB
from app.services.security import CSRF_FIELD
from app.services.waiting import WAIT_LIMITS
from tests.live_server import live_server
from tests.test_add_rows import _in_thread
from tests.test_intake_flow import TEXT, _token
from tests.test_menu import _finish_jobs, _sign_in
from tests.test_publish import _business
from tests.test_spend_cap_samples import cap_reached as cap_reached  # a fixture

LANGS = ["en", "hi", "mr"]


def _t(key: str, lang: str = "en") -> str:
    """The text as it appears in the HTML (apostrophes are escaped)."""
    return str(escape(t(key, lang)))


def _queued_intake(client: TestClient) -> str:
    """Send a typed description without running the job, so the wait page is still waiting."""
    form = {CSRF_FIELD: _token(client), "consent": "yes", "language": "en", "source": "text"}
    response = client.post("/start", data={**form, "text": TEXT}, follow_redirects=False)
    assert response.status_code == 303
    return response.headers["location"].split("/")[2]


def _job(engine: Engine, business_id: int, kind: str, status: str = "running") -> str:
    job_id = f"job-{kind}-{status}-{business_id}"
    with Session(engine) as session:
        session.add(Job(id=job_id, kind=kind, business_id=business_id, status=status))
        session.commit()
    return job_id


def _box(page: str) -> str:
    return page.split('class="progress waiting"')[1].split("</div>\n</div>")[0]


# --- the waiting box, rendered by the server ----------------------------------------------------


@pytest.mark.parametrize("lang", LANGS)
def test_intake_wait_page(client: TestClient, lang: str) -> None:
    token = _queued_intake(client)
    page = client.get(f"/start/{token}/wait?lang={lang}").text
    box = _box(page)
    assert '<span class="spinner" aria-hidden="true"></span>' in box
    assert 'role="status" aria-live="polite"' in page
    assert f'data-wait-limit="{WAIT_LIMITS["intake"]}"' in page
    assert _t("wait.queued", lang) in box and _t("wait.auto", lang) in box
    assert re.search(r'<li class="is-done">' + re.escape(_t("wait.step_received", lang)), box)
    assert f'<li class="is-next">{_t("wait.step_reading", lang)}' in box  # queued: not yet reading
    assert _t("wait.hint", lang) in page  # "a few minutes ... keep this page open"
    assert _t("wait.can_leave", lang) not in page  # not true before a business exists


@pytest.mark.parametrize("lang", LANGS)
def test_owner_page_while_testing_shows_real_stages(
    client: TestClient, engine: Engine, lang: str
) -> None:
    business_id, _ = _sign_in(client, engine)
    pub.build_draft(engine, get_settings(), business_id)
    _job(engine, business_id, SCORE_JOB)
    page = client.get(f"/owner?lang={lang}").text
    box = _box(page)
    assert '<span class="spinner" aria-hidden="true"></span>' in box
    assert _t("owner.scoring", lang) in box
    assert f'<li class="is-current" aria-current="step">{_t("wait.step_testing", lang)}' in box
    assert f'<li class="is-next">{_t("wait.step_score", lang)}' in box
    assert _t("wait.auto", lang) in box and _t("wait.can_leave", lang) in box
    assert "%" not in re.sub(r"<[^>]+>", "", box)  # no made-up percentages


def test_a_failed_test_shows_its_message_and_no_ring(client: TestClient, engine: Engine) -> None:
    business_id, _ = _sign_in(client, engine)
    _job(engine, business_id, SCORE_JOB, "failed")
    page = client.get("/owner").text
    assert _t("owner.score_failed", "en") in page and 'class="spinner"' not in page


@pytest.mark.parametrize("lang", LANGS)
def test_agent_and_domain_waits(client: TestClient, engine: Engine, lang: str) -> None:
    business_id, _ = _sign_in(client, engine)
    job_id = _job(engine, business_id, "agent")
    with Session(engine) as session:
        run = AgentRun(id="ag_wait", business_id=business_id, job_id=job_id, status="running")
        session.add(run)
        session.add(DomainLookup(business_id=business_id, status="running"))
        session.commit()
    page = client.get(f"/owner?lang={lang}").text
    boxes = page.split('class="progress waiting"')[1:]
    kinds = re.findall(r'data-wait-key="(\w+)"', page)
    assert "agent" in kinds and "domains" in kinds
    assert all(_t("wait.can_leave", lang) in b for b in boxes)
    assert _t("domains.checking", lang) in page
    panel = client.get(f"/owner/agent/ag_wait?lang={lang}").text
    assert 'data-wait-key="agent"' in panel and _t("wait.can_leave", lang) in panel


def test_no_time_promise_anywhere(client: TestClient, engine: Engine) -> None:
    token = _queued_intake(client)
    business_id, _ = _sign_in(client, engine)
    _job(engine, business_id, SCORE_JOB)
    for path in (f"/start/{token}/wait", "/owner"):
        text = client.get(path).text.lower()
        assert "about a minute" not in text and "usually takes" not in text


def test_every_posting_button_has_a_busy_label(client: TestClient, engine: Engine) -> None:
    business_id, _ = _sign_in(client, engine)
    pub.build_draft(engine, get_settings(), business_id)
    pub.publish(engine, get_settings(), business_id)
    _finish_jobs(engine)
    pages = {
        "/": ["home.try_button", "contact.send"],
        "/start": ["home.try_button", "start.send"],
        "/owner": ["owner.rebuild", "owner.undo", "owner.offline", "agent.start", "domains.find"],
        "/owner/details": ["details.save"],
        "/owner/menu": ["menu.save"],
        "/owner/publish": ["publish.yes"],
    }
    for path, keys in pages.items():
        page = client.get(path).text
        for key in keys:
            label = re.escape(t(key, "en"))
            busy = r"<button[^>]*data-once=\"[^\"]+\"[^>]*>(?:(?!</button>).)*" + label
            assert re.search(busy, page, re.S), (path, key)


# --- in a real browser ------------------------------------------------------------------------


@pytest.fixture(scope="module")
def browser() -> Iterator[Browser]:
    with sync_playwright() as p:
        launched = p.chromium.launch(
            args=["--use-fake-ui-for-media-stream", "--use-fake-device-for-media-stream"]
        )
        yield launched
        launched.close()


def _owner(page: Page, base: str, link: str, pin: str) -> None:
    page.goto(f"{base}/o/{link}")
    page.fill("#pin", pin)
    page.click("button[type=submit]")
    page.wait_for_url(f"{base}/owner")


@pytest.mark.parametrize("lang", LANGS)
def test_ring_and_late_note_at_both_widths(browser: Browser, client: TestClient, lang: str) -> None:
    token = _in_thread(lambda: _queued_intake(client))
    with live_server() as port:
        page = browser.new_page()
        url = f"http://localhost:{port}/start/{token}/wait?lang={lang}"
        for width in (360, 1280):
            page.set_viewport_size({"width": width, "height": 800})
            page.goto(url)
            page.wait_for_load_state("load")
            assert page.is_visible(".waiting .spinner") and not page.is_visible(".waiting-late")
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
            # Pretend the wait started longer ago than the intake job's own limit.
            page.evaluate(
                """(limit) => { const then = String(Date.now() - limit * 1000 - 5000);
                     for (let i = 0; i < sessionStorage.length; i++) {
                       const k = sessionStorage.key(i);
                       if (k.startsWith('toran-wait:')) sessionStorage.setItem(k, then); } }""",
                WAIT_LIMITS["intake"],
            )
            page.goto(url)
            page.wait_for_load_state("load")
            assert page.is_visible(".waiting-late") and not page.is_visible(".waiting .spinner")
            assert t("wait.late_intake", lang) in page.inner_text(".waiting-late")
            page.evaluate("sessionStorage.clear()")
        page.close()


def test_reduced_motion_keeps_the_ring_still(browser: Browser, client: TestClient) -> None:
    token = _in_thread(lambda: _queued_intake(client))
    with live_server() as port:
        context = browser.new_context(reduced_motion="reduce")
        page = context.new_page()
        page.goto(f"http://localhost:{port}/start/{token}/wait")
        ring = "getComputedStyle(document.querySelector('.spinner')).animationName"
        assert page.evaluate(ring) == "none"
        context.close()


def test_buttons_disable_on_submit_and_come_back_on_pageshow(
    browser: Browser, engine: Engine
) -> None:
    _, link, pin = _business(engine)
    with live_server() as port:
        base = f"http://localhost:{port}"
        page = browser.new_page()
        _owner(page, base, link, pin)
        for path, selector in [
            ("/owner/details", "form[data-live] button[type=submit]"),
            ("/owner/menu", 'form[data-live] button[value="save"]'),
            ("/", "#contact button[type=submit]"),
            ("/owner", "form[action='/owner/draft'] button"),
        ]:
            page.goto(base + path)
            label = page.inner_text(selector)
            # Hold the post back so the busy state can be seen, then come back as the browser would.
            page.evaluate(
                "(s) => document.querySelector(s).form"
                ".addEventListener('submit', e => e.preventDefault())",
                selector,
            )
            if path == "/":
                page.fill("#c-name", "Asha")
                page.fill("#c-contact", "asha@example.com")
                page.fill("#c-message", "Hello")
            page.click(selector)
            page.wait_for_function("(s) => document.querySelector(s).disabled", arg=selector)
            assert page.get_attribute(selector, "aria-busy") == "true", path
            assert page.inner_text(selector) != label, path
            page.evaluate(
                "window.dispatchEvent(new PageTransitionEvent('pageshow', {persisted: true}))"
            )
            assert not page.is_disabled(selector), path
            assert page.inner_text(selector) == label
            assert page.get_attribute(selector, "aria-busy") is None
        # And a real round trip: save, go back, the button works again.
        page.goto(base + "/owner/details")
        with page.expect_navigation():
            page.click("form[data-live] button[type=submit]")
        page.go_back()
        page.wait_for_load_state("load")
        assert not page.is_disabled("form[data-live] button[type=submit]")
        page.close()


def test_with_javascript_off_every_form_posts(
    browser: Browser, client: TestClient, engine: Engine
) -> None:
    _, link, pin = _business(engine)
    with live_server() as port:
        base = f"http://localhost:{port}"
        context = browser.new_context(java_script_enabled=False)
        page = context.new_page()
        page.goto(base + "/")
        page.fill("#c-name", "Asha")
        page.fill("#c-contact", "asha@example.com")
        page.fill("#c-message", "Hello from a phone without JavaScript")
        with page.expect_navigation():
            page.click("#contact button[type=submit]")
        assert t("contact.thanks", "en") in page.inner_text("main")
        page.goto(base + "/start")
        with page.expect_navigation():
            page.click(".try-card button")  # a sample voice note
        assert "/wait" in page.url or "/confirm" in page.url
        _owner(page, base, link, pin)
        for path, selector, done in [
            ("/owner/details", "form[data-live] button[type=submit]", "details.saved"),
            ("/owner/menu", 'form[data-live] button[value="save"]', "menu.same"),
        ]:
            page.goto(base + path)
            with page.expect_navigation():
                page.click(selector)
            assert t(done, "en") in page.inner_text("main"), path
            _in_thread(lambda: _finish_jobs(engine))
        page.goto(base + "/owner")
        with page.expect_navigation():
            page.click("form[action='/owner/draft'] button")
        assert page.url.startswith(base + "/owner")
        context.close()


def test_publish_undo_and_delete_behave_as_before(browser: Browser, engine: Engine) -> None:
    business_id, link, pin = _business(engine)
    _in_thread(lambda: pub.build_draft(engine, get_settings(), business_id))

    def status() -> str:
        with Session(engine) as session:
            business = session.get(Business, business_id)
            return business.status if business else "deleted"

    with live_server() as port:
        base = f"http://localhost:{port}"
        page = browser.new_page()
        _owner(page, base, link, pin)
        page.goto(base + "/owner/publish")
        with page.expect_navigation():
            page.click("form[action='/owner/publish'] button")
        assert _in_thread(status) == "live"
        _in_thread(lambda: _finish_jobs(engine))
        page.goto(base + "/owner")
        with page.expect_navigation():
            page.click("form[action='/owner/undo'] button")
        assert page.url.startswith(base + "/owner")
        # Delete still asks: first the PIN again, then a clear yes or no.
        page.goto(base + "/owner/delete")
        if "/owner/pin" in page.url:  # asked again unless the PIN was entered moments ago
            page.fill("#pin", pin)
            with page.expect_navigation():
                page.click("button[type=submit]")
        assert page.url.endswith("/owner/delete")
        assert t("delete.yes", "en") in page.inner_text("main")
        page.click(f"text={t('delete.no', 'en')}")
        page.wait_for_url(base + "/owner")
        assert _in_thread(status) != "deleted"
        page.close()


def test_sample_cards_post_as_before(browser: Browser) -> None:
    with live_server() as port:
        page = browser.new_page()
        page.goto(f"http://localhost:{port}/start")
        with page.expect_navigation():
            page.click(".try-card button")
        assert re.search(r"/start/[^/]+/(wait|confirm)", page.url)
        page.close()


def test_sample_cards_post_as_before_at_the_spend_cap(browser: Browser, cap_reached: None) -> None:
    with live_server() as port:
        page = browser.new_page()
        page.goto(f"http://localhost:{port}/")
        with page.expect_navigation():
            page.click("#try .try-card button")
        assert re.search(r"/start/[^/]+/(wait|confirm)", page.url)
        page.close()


def _recording(body: bytes | None) -> bytes:
    """The voice-note part of a multipart upload."""
    body = body or b""
    boundary = body.split(b"\r\n", 1)[0]
    part = body.split(b'filename="voice-note', 1)[1].split(b"\r\n\r\n", 1)[1]
    return part.split(b"\r\n" + boundary, 1)[0]


def _fail_first_upload(posts: list[bytes | None]) -> Callable[[Any], None]:
    """A route handler that drops the first upload (as on a lost connection) and lets the rest
    through, keeping each upload's body in posts."""

    def handle(route: Any) -> None:
        if route.request.method != "POST":
            route.continue_()
            return
        posts.append(route.request.post_data_buffer)
        if len(posts) == 1:
            route.abort()
        else:
            route.continue_()

    return handle


def test_record_upload_failure_keeps_the_recording(browser: Browser) -> None:
    with live_server() as port:
        for lang in LANGS:
            context = browser.new_context(permissions=["microphone"])
            page = context.new_page()
            page.goto(f"http://localhost:{port}/start?lang={lang}")
            page.click("label[for=opt-record]")
            page.check("input[name=consent]")
            posts: list[bytes | None] = []
            page.route("**/start", _fail_first_upload(posts))
            page.click("#record-button")
            page.wait_for_timeout(1200)
            page.click("#record-button")  # stop and send
            page.wait_for_function(
                "(label) => document.getElementById('record-button').textContent === label",
                arg=t("record.send_again", lang),
            )
            assert page.inner_text("#record-status") == t("record.failed", lang)
            with page.expect_navigation():
                page.click("#record-button")  # sends the same recording again
            assert len(posts) == 2 and _recording(posts[0]) == _recording(posts[1])
            assert len(_recording(posts[1])) > 1000
            assert re.search(r"/start/[^/]+/(wait|confirm)", page.url)
            context.close()
