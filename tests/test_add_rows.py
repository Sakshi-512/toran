"""After "Add a size or price" or "Add a product", the page lands on the new row with the cursor in
it, and everything already typed is kept. On the intake confirm cards and the menu editor."""

import re
import threading
from collections.abc import Callable, Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient
from playwright.sync_api import Browser, Page, sync_playwright
from sqlalchemy.engine import Engine

from app.services.security import CSRF_FIELD
from tests.live_server import live_server
from tests.test_intake_flow import TEXT, _cards, _start
from tests.test_menu import _form, _products, _sign_in
from tests.test_publish import _business

NEW_ROW = re.compile(r'id="new"')


def _menu_csrf(client: TestClient) -> str:
    page = client.get("/owner/menu").text
    return re.search(rf'name="{CSRF_FIELD}" value="([^"]+)"', page).group(1)  # type: ignore[union-attr]


# --- server side ---------------------------------------------------------------------------------


def test_intake_add_buttons_land_on_the_new_row_and_keep_typed_text(
    client: TestClient, engine: Engine
) -> None:
    token = _start(client, engine, source="text", text=TEXT)
    page, fields = _cards(client, token)
    assert 'formaction="/start/' in page and '/confirm#new"' in page
    assert not NEW_ROW.search(page)
    fields.update(business_name="Typed name", area="Typed area", p0_notes="typed note")
    page = client.post(f"/start/{token}/confirm", data={**fields, "action": "add_option:0"}).text
    assert 'value="Typed name"' in page and 'value="Typed area"' in page
    assert 'value="typed note"' in page
    count = int(fields["p0_option_count"])
    assert re.search(
        rf'<div class="option-row" id="new">\s*<input name="p0_o{count}_label"[^>]*>',
        page,
    )
    products = int(fields["product_count"])
    page = client.post(f"/start/{token}/confirm", data={**fields, "action": "add_product"}).text
    assert 'value="Typed name"' in page
    assert re.search(r'<section class="card product" id="new">', page)
    assert re.search(rf'id="p{products}-name" name="p{products}_name" value=""[^>]*>', page)
    assert len(NEW_ROW.findall(page)) == 1


def test_intake_save_does_not_mark_a_new_row(client: TestClient, engine: Engine) -> None:
    token = _start(client, engine, source="text", text=TEXT)
    _, fields = _cards(client, token)
    page = client.post(f"/start/{token}/confirm", data={**fields, "action": "save"}).text
    assert not NEW_ROW.search(page)


def test_menu_add_buttons_land_on_the_new_row_and_keep_typed_text(
    client: TestClient, engine: Engine
) -> None:
    business_id, _ = _sign_in(client, engine)
    products = _products(engine, business_id)
    products[0]["name"] = "Typed cake"
    products[1]["options"][0]["price"] = 999
    data = {CSRF_FIELD: _menu_csrf(client), **_form(products, action="add_option:1")}
    page = client.post("/owner/menu", data=data).text
    assert 'value="Typed cake"' in page and 'value="999"' in page
    assert 'formaction="/owner/menu#new"' in page
    assert re.search(r'<div class="option-row" id="new">\s*<input name="p1_o2_label"[^>]*>', page)
    data = {CSRF_FIELD: _menu_csrf(client), **_form(products, action="add_product")}
    page = client.post("/owner/menu", data=data).text
    assert 'value="Typed cake"' in page
    assert re.search(r'<section class="card product" id="new">\s*<label for="p3-name">', page)
    assert len(NEW_ROW.findall(page)) == 1


def test_menu_save_errors_do_not_mark_a_new_row(client: TestClient, engine: Engine) -> None:
    business_id, _ = _sign_in(client, engine)
    products = _products(engine, business_id)
    products[0]["options"][0]["price"] = "abc"
    data = {CSRF_FIELD: _menu_csrf(client), **_form(products)}
    response = client.post("/owner/menu", data=data)
    assert response.status_code == 422 and not NEW_ROW.search(response.text)


# --- in a real browser ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def browser() -> Iterator[Browser]:
    with sync_playwright() as p:
        launched = p.chromium.launch()
        yield launched
        launched.close()


VISIBLE_AND_FOCUSED = """
() => {
  const el = document.activeElement;
  if (!el || el.tagName !== 'INPUT') return {ok: false, why: 'no input focused', name: null};
  const r = el.getBoundingClientRect();
  const inside = r.top >= 0 && r.left >= 0 && r.bottom <= innerHeight && r.right <= innerWidth;
  const top = document.elementFromPoint(r.left + r.width / 2, r.top + r.height / 2);
  const covered = !(top === el || el.contains(top));
  const why = inside ? (covered ? 'covered' : '') : 'outside';
  return {ok: inside && !covered, why: why, name: el.name};
}
"""


def _check(page: Page, expected_name: str, where: str) -> list[str]:
    page.wait_for_load_state("load")
    state = page.evaluate(VISIBLE_AND_FOCUSED)
    problems = []
    if state["name"] != expected_name:
        problems.append(f"{where}: focus on {state['name']}, expected {expected_name}")
    elif not state["ok"]:
        problems.append(f"{where}: {expected_name} is {state['why']}")
    if not page.url.endswith("#new"):
        problems.append(f"{where}: url {page.url} has no #new")
    return problems


def _click(page: Page, value: str) -> None:
    with page.expect_navigation():
        page.click(f'button[value="{value}"]')


def _in_thread(work: Callable[[], Any]) -> Any:
    """Run app-side setup off Playwright's event loop."""
    result: dict[str, Any] = {}
    thread = threading.Thread(target=lambda: result.update(value=work()))
    thread.start()
    thread.join(timeout=60)
    return result["value"]


def _walk(
    page: Page, url: str, width: int, where: str, after_save: Callable[[Page], None]
) -> list[str]:
    page.set_viewport_size({"width": width, "height": 800})
    page.goto(url)
    products = int(page.input_value('input[name="product_count"]'))
    options = int(page.input_value('input[name="p0_option_count"]'))
    page.fill('input[name="p0_name"]', "Typed by owner")
    problems: list[str] = []
    _click(page, "add_option:0")
    problems += _check(page, f"p0_o{options}_label", f"{where} first option")
    page.fill(f'input[name="p0_o{options}_label"]', "Large")
    page.fill(f'input[name="p0_o{options}_price"]', "700")
    _click(page, "add_option:0")  # a second tap lands on its own, newer row
    problems += _check(page, f"p0_o{options + 1}_label", f"{where} second option")
    _click(page, "add_product")
    problems += _check(page, f"p{products}_name", f"{where} new product")
    if page.input_value('input[name="p0_name"]') != "Typed by owner":
        problems.append(f"{where}: typed product name lost")
    if page.input_value(f'input[name="p0_o{options}_price"]') != "700":
        problems.append(f"{where}: typed price lost")
    after_save(page)
    if "#new" in page.url or page.locator("#new").count():
        problems.append(f"{where}: still marks a new row after saving ({page.url})")
    if page.evaluate("document.activeElement.tagName") == "INPUT":
        problems.append(f"{where}: a field has focus after saving")
    return problems


@pytest.mark.parametrize("lang", ["en", "hi", "mr"])
def test_intake_cards_in_a_browser(
    browser: Browser, client: TestClient, engine: Engine, lang: str
) -> None:
    def make() -> str:
        token = _start(client, engine, source="text", text=TEXT)
        return token

    token = _in_thread(make)

    def save(page: Page) -> None:
        _click(page, "save")

    problems: list[str] = []
    with live_server() as port:
        context = browser.new_context()
        page = context.new_page()
        for width in (360, 1280):
            url = f"http://localhost:{port}/start/{token}/confirm?lang={lang}"
            problems += _walk(page, url, width, f"intake {lang} {width}px", save)
        context.close()
    assert not problems, "\n".join(problems)


@pytest.mark.parametrize("lang", ["en", "hi", "mr"])
def test_menu_editor_in_a_browser(browser: Browser, engine: Engine, lang: str) -> None:
    _, token, pin = _business(engine)

    def save(page: Page) -> None:
        _click(page, "save")

    problems: list[str] = []
    with live_server() as port:
        base = f"http://localhost:{port}"
        context = browser.new_context()
        page = context.new_page()
        page.goto(f"{base}/o/{token}")
        page.fill("#pin", pin)
        page.click("button[type=submit]")
        page.wait_for_url(f"{base}/owner")
        for width in (360, 1280):
            problems += _walk(
                page, f"{base}/owner/menu?lang={lang}", width, f"menu {lang} {width}px", save
            )
            from tests.test_menu import _finish_jobs

            _finish_jobs(engine)  # the save queued a customer test; let the next save through
        context.close()
    assert not problems, "\n".join(problems)
