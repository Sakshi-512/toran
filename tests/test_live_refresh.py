"""Pages that wait for a background job must update by themselves, in a real browser.

Regression: after "Show me web addresses" the owner page sits at /owner#domains. A plain
<meta http-equiv="refresh" content="5"> reloads "the same URL", and with a #fragment the browser
treats that as a jump within the page, so it never reloaded and the card stayed on
"Checking web addresses with GoDaddy..." although the lookup had finished.
"""

import asyncio
import re
import threading
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from playwright.sync_api import Browser, Page, sync_playwright
from sqlalchemy.engine import Engine
from sqlmodel import Session, select

from app.config import get_settings
from app.models import DomainLookup, utcnow
from app.services import godaddy
from app.services.godaddy import DOMAINS_JOB, domains_runner
from app.services.jobs import JobWorker
from app.services.security import CSRF_FIELD
from tests.live_server import live_server
from tests.test_publish import _business


def _run_domain_jobs(engine: Engine) -> None:
    """Run queued lookups in their own thread, like the app's background worker."""
    worker = JobWorker(engine, {DOMAINS_JOB: domains_runner(engine, get_settings())})
    thread = threading.Thread(target=lambda: asyncio.run(worker.run_until_idle()))
    thread.start()
    thread.join(timeout=60)


@pytest.fixture(scope="module")
def browser() -> Iterator[Browser]:
    with sync_playwright() as p:
        launched = p.chromium.launch()
        yield launched
        launched.close()


def _signed_in_page(browser: Browser, base: str, token: str, pin: str) -> Page:
    page = browser.new_context(viewport={"width": 360, "height": 800}).new_page()
    page.goto(f"{base}/o/{token}")
    page.fill("#pin", pin)
    page.click("button[type=submit]")
    page.wait_for_url(f"{base}/owner")
    return page


@pytest.mark.browser
def test_web_address_card_updates_by_itself_after_the_lookup(
    engine: Engine, browser: Browser
) -> None:
    _, token, pin = _business(engine)
    with live_server() as port:
        base = f"http://localhost:{port}"
        page = _signed_in_page(browser, base, token, pin)
        page.click("text=Show me web addresses")
        page.wait_for_url(re.compile(r".*/owner#domains$"))
        assert "Checking web addresses with GoDaddy" in page.inner_text("#domains")
        _run_domain_jobs(engine)  # the lookup finishes in the background, page untouched
        # No navigation by us: the page has to reload itself to show this.
        page.wait_for_selector("#domains >> text=I want this one", timeout=15_000)
        card = page.inner_text("#domains")
        assert "Checking web addresses" not in card
        assert "$12.99" in card and "price before tax" in card
        page.context.close()


def test_finished_lookup_shows_results_not_checking(client: TestClient, engine: Engine) -> None:
    business_id, token, pin = _business(engine)
    link = f"/o/{token}"
    csrf = re.search(rf'name="{CSRF_FIELD}" value="([^"]+)"', client.get(link).text).group(1)  # type: ignore[union-attr]
    client.post(link, data={CSRF_FIELD: csrf, "pin": pin})
    client.post("/owner/domains", data={CSRF_FIELD: csrf})
    _run_domain_jobs(engine)
    page = client.get("/owner").text
    card = page.split('id="domains"')[1].split("</section>")[0]
    assert "Checking web addresses" not in card
    assert "I want this one" in card and "$12.99" in card and "price before tax" in card
    with Session(engine) as session:
        assert session.exec(select(DomainLookup)).one().business_id == business_id


def test_waiting_pages_refresh_without_relying_on_the_url(
    client: TestClient, engine: Engine
) -> None:
    """While a lookup runs, the page reloads itself with a script (keeps the owner's place, works
    with a #fragment) and, without JavaScript, with a refresh to the address without the
    fragment."""
    business_id, token, pin = _business(engine)
    link = f"/o/{token}"
    csrf = re.search(rf'name="{CSRF_FIELD}" value="([^"]+)"', client.get(link).text).group(1)  # type: ignore[union-attr]
    client.post(link, data={CSRF_FIELD: csrf, "pin": pin})
    with Session(engine) as session:
        session.add(DomainLookup(business_id=business_id, status="running"))
        session.commit()
    page = client.get("/owner").text
    assert 'name="auto-refresh" content="5"' in page and "/static/autorefresh.js" in page
    assert re.search(
        r'<noscript><meta http-equiv="refresh" content="5; url=/owner[^#"]*"></noscript>', page
    )
    assert '<meta http-equiv="refresh" content="5">' not in page


def test_a_stuck_lookup_shows_the_unavailable_note(client: TestClient, engine: Engine) -> None:
    from datetime import timedelta

    business_id, token, pin = _business(engine)
    link = f"/o/{token}"
    csrf = re.search(rf'name="{CSRF_FIELD}" value="([^"]+)"', client.get(link).text).group(1)  # type: ignore[union-attr]
    client.post(link, data={CSRF_FIELD: csrf, "pin": pin})
    with Session(engine) as session:
        stuck = utcnow() - timedelta(seconds=godaddy.JOB_TIMEOUT_SECONDS + 60)
        session.add(DomainLookup(business_id=business_id, status="running", created_at=stuck))
        session.commit()
    card = client.get("/owner").text.split('id="domains"')[1].split("</section>")[0]
    assert "Checking web addresses" not in card
    assert "not available right now" in card
    assert "Show me web addresses" in card  # the owner can try again
    client.post("/owner/domains", data={CSRF_FIELD: csrf})
    with Session(engine) as session:
        statuses = [row.status for row in session.exec(select(DomainLookup)).all()]
    assert statuses == ["timed_out", "running"]  # a new lookup started, not the stuck one
