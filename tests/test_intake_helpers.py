from datetime import timedelta

import pytest
from sqlalchemy.engine import Engine
from sqlmodel import Session

from app.models import Business, utcnow
from app.services import web_address
from app.services.accuracy import accuracy, correction_rate, corrections, flatten
from app.services.auth import (
    business_for_link,
    check_pin,
    hash_pin,
    issue_access,
    new_pin,
    pin_matches,
)
from app.services.numbers import ascii_digits, digits_only, numbers_in
from scripts.check_private_files import problem

# --- numbers --------------------------------------------------------------------------------


def test_devanagari_digits_and_indian_formats() -> None:
    assert ascii_digits("४५० रुपये") == "450 रुपये"
    assert numbers_in("केक ४५० आणि १,२०० रुपये") == {"450", "1200"}
    assert numbers_in("1,500 or 1500 or ₹1,25,000") == {"1500", "125000"}
    assert digits_only("९०००० १२३४५") == "9000012345"
    assert numbers_in("0450") == {"450"}


# --- web addresses ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("सुनीता ब्युटी पार्लर", "sunita-beauty-parlour"),
        ("आशा केक्स", "asha-cakes"),
        ("शर्मा क्लासेस", "sharma-classes"),
        ("Asha's Home Bakery", "ashas-home-bakery"),
        ("मीना टेलर्स", "mina-tailors"),
    ],
)
def test_readable_web_address_from_name(name: str, expected: str) -> None:
    assert web_address.clean(web_address.transliterate(name)) == expected


@pytest.mark.parametrize(
    "bad", ["ab", "-shop", "shop-", "sh op", "shop--two", "www", "a" * 41, "शॉप"]
)
def test_invalid_web_addresses(bad: str) -> None:
    assert web_address.problem(bad) == "intake.address_invalid"


def test_suggest_prefers_claude_then_name_and_stays_unique(engine: Engine) -> None:
    with Session(engine) as session:
        assert web_address.suggest(session, "सुनीता ब्युटी पार्लर", "") == "sunita-beauty-parlour"
        assert (
            web_address.suggest(session, "x", "Sunita Beauty Parlour!") == "sunita-beauty-parlour"
        )
        session.add(Business(slug="sunita-beauty-parlour", display_name="S"))
        session.commit()
        assert web_address.suggest(session, "सुनीता ब्युटी पार्लर") == "sunita-beauty-parlour-2"
        assert web_address.suggest(session, "!!") == "my-business"


# --- auth ------------------------------------------------------------------------------------


def test_pin_is_four_digits_and_hashed() -> None:
    pin = new_pin()
    assert len(pin) == 4 and pin.isdigit()
    stored = hash_pin(pin)
    assert pin not in stored and stored.startswith("scrypt$")
    assert pin_matches(pin, stored)
    assert not pin_matches("0000" if pin != "0000" else "1111", stored)
    assert hash_pin(pin) != stored  # salted


def _business(engine: Engine) -> tuple[int, str, str]:
    with Session(engine) as session:
        business = Business(slug="shop", display_name="Shop")
        access = issue_access(business)
        session.add(business)
        session.commit()
        assert business.id is not None
        assert business.owner_link_hash != access.link_token
        return business.id, access.link_token, access.pin


def test_link_and_pin(engine: Engine) -> None:
    business_id, token, pin = _business(engine)
    with Session(engine) as session:
        business = business_for_link(session, token)
        assert business is not None and business.id == business_id
        assert business_for_link(session, token + "x") is None
        assert check_pin(session, business, pin) == "ok"


def test_five_wrong_pins_lock_for_fifteen_minutes(engine: Engine) -> None:
    _, token, pin = _business(engine)
    wrong = "0000" if pin != "0000" else "1111"
    now = utcnow()
    with Session(engine) as session:
        business = business_for_link(session, token)
        assert business is not None
        results = [check_pin(session, business, wrong, now) for _ in range(5)]
        assert results == ["wrong"] * 4 + ["locked"]
        assert check_pin(session, business, pin, now + timedelta(minutes=14)) == "locked"
        assert check_pin(session, business, pin, now + timedelta(minutes=16)) == "ok"
        assert business.pin_failures == 0


def test_deleted_business_link_stops_working(engine: Engine) -> None:
    _, token, _ = _business(engine)
    with Session(engine) as session:
        business = business_for_link(session, token)
        assert business is not None
        business.deleted_at = utcnow()
        session.add(business)
        session.commit()
        assert business_for_link(session, token) is None


# --- accuracy ---------------------------------------------------------------------------------

GUESS = {
    "business_name": "Asha Bakery",
    "business_type": "bakery",
    "area": "Kothrud",
    "delivery_areas": ["Kothrud"],
    "phone": "",
    "products": [
        {"name": "Chocolate cake", "unit": "kg", "options": [{"label": "1 kg", "price": 850}]},
        {"name": "Brownie", "unit": "piece", "options": [{"label": "", "price": 60}]},
    ],
}
FINAL = {
    **GUESS,
    "phone": "+919000012345",
    "products": [
        {"name": "Chocolate Cake", "unit": "kg", "options": [{"label": "1 KG", "price": 900}]},
        {"name": "Modak", "unit": "box", "options": [{"label": "", "price": 330}]},
    ],
}


def test_flatten_matches_products_by_normalised_name() -> None:
    assert "products[chocolate cake].options[1 kg].price" in flatten(FINAL)


def test_corrections_list_each_changed_field() -> None:
    changed = {c["field"]: (c["before"], c["after"]) for c in corrections(GUESS, FINAL)}
    assert changed["phone"] == (None, "+919000012345")
    assert changed["products[chocolate cake].options[1 kg].price"] == (850, 900)
    assert "products[brownie].unit" in changed and "products[modak].unit" in changed
    assert "business_name" not in changed


def test_accuracy_and_correction_rate() -> None:
    result = accuracy(GUESS, FINAL)
    assert (result.prices_total, result.prices_exact) == (2, 0)
    assert (result.products_missed, result.products_made_up) == (1, 1)
    assert 0 < result.field_accuracy < 1
    assert correction_rate([(GUESS, GUESS)]) == 0.0
    assert correction_rate([(GUESS, FINAL)]) == pytest.approx(1 - result.field_accuracy)


# --- privacy hook ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    [
        "voice_notes/real-owner.ogg",
        "evals/private/real-case.json",
        "notes/recording.m4a",
        "photos/menu.jpg",
        "data/app.db",
        "evals/reports/eval.md",
        "uploads/x.png",
    ],
)
def test_private_files_blocked(path: str) -> None:
    assert problem(path) is not None


@pytest.mark.parametrize(
    "path",
    [
        "app/static/app.css",
        "docs/screenshots/warm-360.png",
        "evals/cases/files/meena-rate-card.png",
        "app/templates/sites/sample-photos/bakery.jpg",
        "tests/fixtures/stt/sample-en.json",
        "evals/cases/sample-en.json",
    ],
)
def test_allowed_files(path: str) -> None:
    assert problem(path) is None
