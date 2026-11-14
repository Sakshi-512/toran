from typing import Any

import pytest
from pydantic import ValidationError

from app.schemas import BusinessProfile, ProductOption, normalise_phone


def _profile(**overrides: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "business_name": "Sunita's Cakes",
        "business_type": "bakery",
        "area": "Kothrud, Pune",
        "delivery_areas": ["Kothrud", "Karve Nagar", "Kothrud"],
        "languages": ["MR", "en"],
        "products": [
            {
                "name": "Ukadiche modak",
                "unit": "box",
                "options": [{"label": "box of 11", "price": 350}],
                "notes": "",
            }
        ],
        "order_methods": ["whatsapp"],
        "lead_time": "2 days",
        "phone": "98765 43210",
        "missing": [],
    }
    data.update(overrides)
    return data


def test_valid_profile_is_normalised() -> None:
    profile = BusinessProfile.model_validate(_profile())
    assert profile.delivery_areas == ["Kothrud", "Karve Nagar"]
    assert profile.languages == ["mr", "en"]
    assert profile.phone == "+919876543210"
    assert profile.products[0].options[0].price == 350


def test_empty_profile_has_defaults() -> None:
    profile = BusinessProfile()
    assert profile.order_methods == ["whatsapp"]
    assert profile.business_type == "other"


def test_unknown_fields_rejected() -> None:
    with pytest.raises(ValidationError):
        BusinessProfile.model_validate(_profile(reviews=["Best cakes!"]))


@pytest.mark.parametrize("bad", ["shop", "Bakery "])
def test_business_type_must_be_known(bad: str) -> None:
    with pytest.raises(ValidationError):
        BusinessProfile.model_validate(_profile(business_type=bad))


def test_unit_must_be_known() -> None:
    product = {"name": "Cake", "unit": "slice", "options": []}
    with pytest.raises(ValidationError):
        BusinessProfile.model_validate(_profile(products=[product]))


@pytest.mark.parametrize("price", [-1, 12.5, True, "abc", 10_000_001])
def test_bad_prices_rejected(price: object) -> None:
    with pytest.raises(ValidationError):
        ProductOption.model_validate({"label": "1 kg", "price": price})


@pytest.mark.parametrize(("price", "expected"), [(0, 0), (450, 450), (450.0, 450), ("450", 450)])
def test_good_prices_accepted(price: object, expected: int) -> None:
    assert ProductOption.model_validate({"label": "1 kg", "price": price}).price == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("9876543210", "+919876543210"),
        ("+91 98765-43210", "+919876543210"),
        ("09876543210", "+919876543210"),
        ("919876543210", "+919876543210"),
        ("+447911123456", "+447911123456"),
        ("", ""),
    ],
)
def test_phone_normalisation(raw: str, expected: str) -> None:
    assert normalise_phone(raw) == expected


@pytest.mark.parametrize("raw", ["12345", "5876543210", "+12", "phone me"])
def test_bad_phone_rejected(raw: str) -> None:
    with pytest.raises(ValueError):
        normalise_phone(raw)


@pytest.mark.parametrize(
    ("business_type", "key"),
    [
        ("beauty", "cta.book_whatsapp"),
        ("tuition", "cta.book_whatsapp"),
        ("mehendi", "cta.book_whatsapp"),
        ("bakery", "cta.order_whatsapp"),
        ("tailoring", "cta.order_whatsapp"),
    ],
)
def test_call_to_action_by_type(business_type: str, key: str) -> None:
    assert BusinessProfile.model_validate(_profile(business_type=business_type)).cta_key == key
