"""The business name rule (server side), the required markers, input hints and Back links."""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.engine import Engine

from app.services.field_rules import NAME_MAX, business_name_problem
from tests.test_intake_flow import TEXT, _cards, _confirm, _start

ACCEPTED = [
    "Kashvi Beauty Parlour",
    "Asha's Home Bakery",
    "Meena’s Tailors",
    "Sharma Tuition Classes",
    "Sunita Beauty Parlour",
    "Rukhsar Mehendi Art",
    "Shree Ganesh Mehendi / Beauty",
    "Cheryl & Co.",
    "Savitri Gruh Udyog",
    "आशा होम बेकरी",
    "सुनीताचे ब्युटी पार्लर",
    "कऱ्हाडे क्लासेस",  # needs the zero-width joiner on some keyboards
    "Kar‍hade Classes",
    "Bakery 24",
    "A" * NAME_MAX,
]

REFUSED = [
    ("klmeds.,c≤.,/z.c,/scsc", "intake.name_chars"),
    ("<script>", "intake.name_chars"),
    ("12345", "intake.name_letter"),
    ("A", "intake.name_length"),
    ("A" * (NAME_MAX + 1), "intake.name_length"),
    ("Asha\x07 Bakery", "intake.name_chars"),
    ("Asha @ Bakery", "intake.name_chars"),
    ("--", "intake.name_letter"),
]


@pytest.mark.parametrize("name", ACCEPTED)
def test_realistic_names_are_accepted(name: str) -> None:
    assert business_name_problem(name) is None


@pytest.mark.parametrize(("name", "key"), REFUSED)
def test_other_names_are_refused_with_a_reason(name: str, key: str) -> None:
    assert business_name_problem(name) == key


@pytest.mark.parametrize(
    ("lang", "words"),
    [("en", "Business name"), ("hi", "व्यवसाय का नाम"), ("mr", "व्यवसायाचे नाव")],
)
def test_confirm_refuses_gibberish_and_names_the_field(
    client: TestClient, engine: Engine, lang: str, words: str
) -> None:
    token = _start(client, engine, source="text", text=TEXT)
    _, fields = _cards(client, token)
    fields.update(business_name="klmeds.,c≤.,/z.c,/scsc", action="confirm")
    response = client.post(f"/start/{token}/confirm?lang={lang}", data=fields)
    assert response.status_code == 422
    assert words in response.text and 'value="klmeds.,c≤.,/z.c,/scsc"' in response.text


def test_confirm_accepts_a_devanagari_name(client: TestClient, engine: Engine) -> None:
    token = _start(client, engine, source="text", text=TEXT)
    _, fields = _cards(client, token)
    fields.update(business_name="आशा होम बेकरी")
    response = _confirm(client, token, fields)
    assert response.status_code == 200 and "field-error" not in response.text
    assert 'class="secret-value pin"' in response.text  # the done page, with the owner's PIN


def test_required_markers_and_hints_on_the_confirm_page(client: TestClient, engine: Engine) -> None:
    token = _start(client, engine, source="text", text=TEXT)
    page, _ = _cards(client, token)
    assert page.count('aria-required="true"') == 2
    assert 'name="business_name"' in page.split('aria-required="true"')[0].rsplit("<input", 1)[1]
    assert page.count('<span class="req" aria-hidden="true">*</span>') == 3  # legend + 2 labels
    assert 'class="req-legend"' in page and " required" not in page.replace("aria-required", "")
    assert f'maxlength="{NAME_MAX}"' in page
    assert 'type="tel" inputmode="tel" autocomplete="tel"' in page


@pytest.mark.parametrize(
    ("path", "href"), [("/start", "/"), ("/owner/details", "/owner"), ("/owner/menu", "/owner")]
)
def test_back_links(client: TestClient, engine: Engine, path: str, href: str) -> None:
    from tests.test_menu import _sign_in

    _sign_in(client, engine)
    page = client.get(path).text
    assert f'<a class="back-link" href="{href}">' in page


def test_confirm_back_link(client: TestClient, engine: Engine) -> None:
    token = _start(client, engine, source="text", text=TEXT)
    page, _ = _cards(client, token)
    assert '<a class="back-link" href="/start">' in page


@pytest.mark.parametrize("lang", ["en", "hi", "mr"])
def test_confirm_tab_title_is_plain_text(client: TestClient, engine: Engine, lang: str) -> None:
    from playwright.sync_api import sync_playwright

    from app.services.i18n import t
    from tests.live_server import live_server
    from tests.test_add_rows import _in_thread

    token = _in_thread(lambda: _start(client, engine, source="text", text=TEXT))
    with sync_playwright() as p, live_server() as port:
        browser = p.chromium.launch()
        page = browser.new_page()
        page.goto(f"http://localhost:{port}/start/{token}/confirm?lang={lang}")
        title = page.evaluate("document.title")
        browser.close()
    assert "<" not in title
    assert title.startswith(t("confirm.title", lang) + " · ")
