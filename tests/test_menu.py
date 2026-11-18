"""Edit my menu and prices: checks, versions, restore, the PIN rules and the busy rule."""

import re
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.engine import Engine
from sqlmodel import Session, select

from app.config import get_settings
from app.models import AgentRun, Job, ProfileVersion
from app.services import auth
from app.services import menu as svc
from app.services import publish as pub
from app.services.intake import CardErrors
from app.services.score_runs import SCORE_JOB
from app.services.security import CSRF_FIELD
from tests.test_publish import _business

REALISTIC = [
    "Combo No. 2",
    "Thali No 1",
    "Box of 10",
    "Birthday hamper 2",
    "Mehendi bridal package 1",
    "Family pack 500 g",
]
ADDRESSES = ["Flat 4, Sai Society", "House no 12 Baner Road"]


def _csrf(client: TestClient, path: str = "/owner/menu") -> str:
    page = client.get(path).text
    return re.search(rf'name="{CSRF_FIELD}" value="([^"]+)"', page).group(1)  # type: ignore[union-attr]


def _sign_in(client: TestClient, engine: Engine, **kwargs: Any) -> tuple[int, str]:
    business_id, token, pin = _business(engine, **kwargs)
    link = f"/o/{token}"
    client.post(link, data={CSRF_FIELD: _csrf(client, link), "pin": pin})
    return business_id, pin


def _products(engine: Engine, business_id: int) -> list[dict[str, Any]]:
    with Session(engine) as session:
        return svc.current_products(session, business_id)


def _versions(engine: Engine, business_id: int) -> list[ProfileVersion]:
    with Session(engine) as session:
        rows = session.exec(
            select(ProfileVersion).where(ProfileVersion.business_id == business_id)
        ).all()
        return sorted(rows, key=lambda v: v.version)


def _form(products: list[dict[str, Any]], **extra: str) -> dict[str, str]:
    """The menu cards as the browser would send them."""
    data: dict[str, str] = {"product_count": str(len(products)), "action": "save", **extra}
    for i, p in enumerate(products):
        data[f"p{i}_name"], data[f"p{i}_unit"] = p["name"], p["unit"]
        data[f"p{i}_notes"] = p.get("notes", "")
        data[f"p{i}_option_count"] = str(len(p["options"]))
        for j, o in enumerate(p["options"]):
            data[f"p{i}_o{j}_label"], data[f"p{i}_o{j}_price"] = o["label"], str(o["price"])
    return data


def _post(client: TestClient, products: list[dict[str, Any]], **extra: str) -> Any:
    data = {CSRF_FIELD: _csrf(client), **_form(products, **extra)}
    return client.post("/owner/menu", data=data, follow_redirects=False)


def _finish_jobs(engine: Engine) -> None:
    """Pretend the queued customer tests have finished."""
    with Session(engine) as session:
        for job in session.exec(select(Job)).all():
            job.status = "succeeded"
            session.add(job)
        session.commit()


def _price(products: list[dict[str, Any]], i: int, j: int, price: object) -> None:
    products[i]["options"][j]["price"] = price


@pytest.fixture
def pin_expired(monkeypatch: pytest.MonkeyPatch) -> None:
    """The PIN entered at sign-in no longer counts as recent."""
    monkeypatch.setattr(auth, "RECENT_PIN_MINUTES", 0)


# --- checks ------------------------------------------------------------------------------------


@pytest.mark.parametrize("price", ["abc", "-5", "12.50", "1e3"])
def test_bad_prices_are_refused(client: TestClient, engine: Engine, price: str) -> None:
    business_id, _ = _sign_in(client, engine)
    products = _products(engine, business_id)
    _price(products, 0, 0, price)
    response = _post(client, products)
    assert response.status_code == 422
    assert "Please write the price as a number" in response.text
    assert len(_versions(engine, business_id)) == 1


def test_a_small_price_change_saves_a_new_version_without_pin(
    client: TestClient, engine: Engine, pin_expired: None
) -> None:
    business_id, _ = _sign_in(client, engine)
    products = _products(engine, business_id)
    _price(products, 0, 0, 480)  # 450 -> 480
    response = _post(client, products)
    assert response.status_code == 303 and response.headers["location"] == "/owner/menu?done=saved"
    versions = _versions(engine, business_id)
    assert [v.version for v in versions] == [1, 2]
    assert versions[1].profile["products"][0]["options"][0]["price"] == 480
    assert versions[1].created_by == "owner"
    page = client.get("/owner/menu?done=saved").text
    assert "Changes are in your draft. Make your site live to show them to customers." in page


def test_owner_can_add_and_remove_products_and_options(client: TestClient, engine: Engine) -> None:
    business_id, _ = _sign_in(client, engine)
    products = _products(engine, business_id)
    data = _form(products, action="add_product")
    page = client.post("/owner/menu", data={CSRF_FIELD: _csrf(client), **data}).text
    assert 'name="p3_name"' in page
    data = _form(products, action="add_option:1")
    page = client.post("/owner/menu", data={CSRF_FIELD: _csrf(client), **data}).text
    assert 'name="p1_o2_label"' in page
    products.append(
        {"name": "Shankarpali", "unit": "box", "options": [{"label": "", "price": 120}]}
    )
    _post(client, products, p2_remove="yes")  # remove Nankhatai, add Shankarpali
    names = [p["name"] for p in _products(engine, business_id)]
    assert "Nankhatai" not in names and "Shankarpali" in names


@pytest.mark.parametrize("name", REALISTIC)
def test_realistic_names_are_not_refused(name: str) -> None:
    errors = CardErrors()
    form = _form(
        [{"name": name, "unit": "box", "notes": name, "options": [{"label": name, "price": 100}]}]
    )
    svc.products_from_form(form, errors)
    assert errors.errors == {}


@pytest.mark.parametrize("value", ADDRESSES)
def test_address_like_values_are_refused_naming_the_field(value: str) -> None:
    for field, key in (("name", "p0_name"), ("label", "p0_o0_label"), ("notes", "p0_notes")):
        product = {
            "name": "Cake",
            "unit": "box",
            "notes": "",
            "options": [{"label": "", "price": 100}],
        }
        if field == "label":
            product["options"][0]["label"] = value  # type: ignore[index]
        else:
            product[field] = value
        errors = CardErrors()
        svc.products_from_form(_form([product]), errors)
        assert errors.errors == {key: f"menu.address_in_{field}"}


@pytest.mark.parametrize(
    ("lang", "words"),
    [
        ("en", "Product name: this looks like an address"),
        ("hi", "प्रोडक्ट का नाम"),
        ("mr", "उत्पादनाचे नाव"),
    ],
)
def test_address_name_refused_on_the_page_in_three_languages(
    client: TestClient, engine: Engine, lang: str, words: str
) -> None:
    business_id, _ = _sign_in(client, engine)
    client.get(f"/owner/menu?lang={lang}")
    products = _products(engine, business_id)
    products[0]["name"] = "Flat 4, Sai Society"
    response = _post(client, products)
    assert response.status_code == 422 and words in response.text
    assert len(_versions(engine, business_id)) == 1


def test_names_are_escaped(client: TestClient, engine: Engine) -> None:
    business_id, _ = _sign_in(client, engine)
    products = _products(engine, business_id)
    products[0]["name"] = "<script>alert(1)</script> cake"
    assert _post(client, products).status_code == 303
    assert "<script>alert(1)</script>" not in client.get("/owner/menu").text


# --- the PIN rules -----------------------------------------------------------------------------


def _many_changes(products: list[dict[str, Any]]) -> None:
    """Six prices, each changed by about 10 percent."""
    products.append(
        {"name": "Ladoo", "unit": "box", "notes": "", "options": [{"label": "", "price": 100}]}
    )
    for i, p in enumerate(products):
        for j, o in enumerate(p["options"]):
            _price(products, i, j, int(o["price"]) + int(o["price"]) // 10 + 1)


def test_more_than_five_price_changes_need_the_pin(
    client: TestClient, engine: Engine, pin_expired: None
) -> None:
    business_id, pin = _sign_in(client, engine)
    products = _products(engine, business_id)
    _many_changes(products)
    assert svc.needs_pin(_products(engine, business_id), products)
    response = _post(client, products)
    assert response.status_code == 401 and 'name="pin"' in response.text
    assert len(_versions(engine, business_id)) == 1
    assert _post(client, products, pin="0000" if pin != "0000" else "1111").status_code == 401
    assert _post(client, products, pin=pin).status_code == 303
    assert len(_versions(engine, business_id)) == 2


def test_one_large_price_change_needs_the_pin(
    client: TestClient, engine: Engine, pin_expired: None
) -> None:
    business_id, pin = _sign_in(client, engine)
    products = _products(engine, business_id)
    _price(products, 2, 0, 300)  # 180 -> 300 is 67 percent
    assert _post(client, products).status_code == 401
    assert _post(client, products, pin=pin).status_code == 303


def test_pin_rule_thresholds() -> None:
    old: list[dict[str, Any]] = [
        {"name": "A", "unit": "box", "notes": "", "options": [{"label": "", "price": 100}]}
    ]
    new: list[dict[str, Any]] = [{**old[0], "options": [{"label": "", "price": 150}]}]
    assert not svc.needs_pin(old, new)  # exactly 50 percent is allowed
    new[0]["options"][0]["price"] = 151
    assert svc.needs_pin(old, new)
    five = [
        {"name": f"P{i}", "unit": "box", "notes": "", "options": [{"label": "", "price": 10}]}
        for i in range(5)
    ]
    assert not svc.needs_pin([], five)  # five new prices
    assert svc.needs_pin([], [*five, {**five[0], "name": "P5"}])  # six


def test_live_business_compares_against_the_live_profile(
    client: TestClient, engine: Engine, pin_expired: None
) -> None:
    business_id, _ = _sign_in(client, engine)
    pub.build_draft(engine, get_settings(), business_id)
    pub.publish(engine, get_settings(), business_id)
    _finish_jobs(engine)
    products = _products(engine, business_id)
    _price(products, 2, 0, 230)  # 180 -> 230, small
    assert _post(client, products).status_code == 303
    _finish_jobs(engine)
    _price(products, 2, 0, 280)  # 230 -> 280 is small, but 180 -> 280 (live) is large
    assert _post(client, products).status_code == 401


# --- busy, other businesses, rebuild and re-score ------------------------------------------------


def test_refused_while_a_scoring_job_or_agent_run_is_running(
    client: TestClient, engine: Engine
) -> None:
    business_id, _ = _sign_in(client, engine)
    products = _products(engine, business_id)
    _price(products, 0, 0, 460)
    with Session(engine) as session:
        session.add(Job(id="job-busy", kind=SCORE_JOB, business_id=business_id, status="running"))
        session.commit()
    response = _post(client, products)
    assert response.status_code == 409 and "Please wait a minute" in response.text
    page = client.get("/owner/menu?lang=mr").text
    assert "एक मिनिट थांबून" in page
    _finish_jobs(engine)
    with Session(engine) as session:
        session.add(Job(id="job-agent", kind="agent", business_id=business_id, status="running"))
        session.add(
            AgentRun(id="ag_busy", business_id=business_id, job_id="job-agent", status="running")
        )
        session.commit()
    assert _post(client, products).status_code == 409
    assert len(_versions(engine, business_id)) == 1


def test_saving_rebuilds_and_rescores_the_draft_only(client: TestClient, engine: Engine) -> None:
    business_id, _ = _sign_in(client, engine)
    other_id, _, _ = _business(engine, slug="other-shop")
    settings = get_settings()
    pub.build_draft(engine, settings, business_id)
    pub.publish(engine, settings, business_id)
    _finish_jobs(engine)
    with Session(engine) as session:
        slug = pub._business(session, business_id).slug
    paths = pub.folders(settings, slug)
    live_before = (paths.live / "index.html").read_text(encoding="utf-8")
    products = _products(engine, business_id)
    products[2]["name"] = "Nankhatai special"
    assert _post(client, products).status_code == 303
    assert "Nankhatai special" in (paths.draft / "index.html").read_text(encoding="utf-8")
    assert (paths.live / "index.html").read_text(encoding="utf-8") == live_before
    with Session(engine) as session:
        jobs = session.exec(select(Job).where(Job.kind == SCORE_JOB, Job.status == "queued")).all()
    assert [(j.business_id, (j.payload or {}).get("kind")) for j in jobs] == [
        (business_id, "draft")
    ]
    assert len(_versions(engine, other_id)) == 1


def test_another_businesss_menu_cannot_be_reached(client: TestClient, engine: Engine) -> None:
    _sign_in(client, engine)
    other_id, _, _ = _business(engine, slug="other-shop")
    assert "Nankhatai" in client.get("/owner/menu").text
    client.cookies.clear()
    assert client.get("/owner/menu", follow_redirects=False).status_code == 401
    assert len(_versions(engine, other_id)) == 1


# --- restore -----------------------------------------------------------------------------------


def test_restore_brings_back_the_previous_menu_and_twice_swaps_back(
    client: TestClient, engine: Engine
) -> None:
    business_id, _ = _sign_in(client, engine)
    assert "Restore the previous menu" not in client.get("/owner/menu").text
    original = _products(engine, business_id)
    products = _products(engine, business_id)
    _price(products, 0, 0, 470)
    products.pop()  # remove Nankhatai
    _post(client, products)
    _finish_jobs(engine)
    page = client.get("/owner/menu").text
    assert re.search(r"Restore the previous menu \(\d\d/\d\d/\d{4}, \d\d:\d\d, 3 products\)", page)
    restore = client.post(
        "/owner/menu/restore", data={CSRF_FIELD: _csrf(client)}, follow_redirects=False
    )
    assert restore.headers["location"] == "/owner/menu?done=restored"
    assert _products(engine, business_id) == original
    _finish_jobs(engine)
    assert "2 products)" in client.get("/owner/menu").text
    client.post("/owner/menu/restore", data={CSRF_FIELD: _csrf(client)})
    assert _products(engine, business_id) == products
    assert [v.version for v in _versions(engine, business_id)] == [1, 2, 3, 4]


def test_sample_businesses_can_edit_their_menu(client: TestClient, engine: Engine) -> None:
    business_id, _ = _sign_in(client, engine, is_sample=True)
    products = _products(engine, business_id)
    _price(products, 1, 0, 340)
    assert _post(client, products).status_code == 303
    assert _versions(engine, business_id)[-1].profile["products"][1]["options"][0]["price"] == 340
