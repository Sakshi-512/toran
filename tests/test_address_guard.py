import pytest

from app.services.address_guard import address_reason, looks_like_address

PUNE_AREAS = [
    "Kothrud",
    "Karve Nagar",
    "Baner",
    "Aundh",
    "Sinhagad Road",
    "FC Road",
    "Viman Nagar",
    "Kothrud, Pune",
    "Camp",
    "Wanowrie",
    "कोथरूड",
    "कर्वे नगर",
]


@pytest.mark.parametrize("area", PUNE_AREAS)
def test_area_names_are_not_flagged(area: str) -> None:
    assert address_reason(area) is None


@pytest.mark.parametrize(
    "text",
    [
        "Flat 12, Shanti Society, Kothrud",
        "B-204 Sunrise Apartments",
        "12/3 Karve Road",
        "Kothrud, Pune 411038",
        "Pune 411 038",
        "House no 5, Baner",
        "Lane 7, Koregaon Park",
        "#45 MG Road",
        "12, FC Road",
        "Building 3, Viman Nagar",
        "फ्लॅट 12, कोथरूड",
        "शांती सोसायटी, कर्वे नगर",
    ],
)
def test_addresses_are_flagged(text: str) -> None:
    assert looks_like_address(text)
