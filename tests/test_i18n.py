import json

import pytest

from app.services.i18n import I18N_DIR, SUPPORTED, normalise_language, t


def test_all_languages_have_the_same_keys() -> None:
    keys = {
        lang: set(json.loads((I18N_DIR / f"{lang}.json").read_text("utf-8"))) for lang in SUPPORTED
    }
    assert keys["hi"] == keys["en"]
    assert keys["mr"] == keys["en"]


def test_no_empty_strings() -> None:
    for lang in SUPPORTED:
        data = json.loads((I18N_DIR / f"{lang}.json").read_text("utf-8"))
        assert all(value.strip() for value in data.values()), lang


def test_lookup_and_formatting() -> None:
    assert t("home.try_now", "mr") == "आत्ताच वापरून पाहा"
    assert t("home.title", "en", app_name="Shop") == "Shop"


def test_fallbacks() -> None:
    assert t("home.try_now", "fr") == t("home.try_now", "en")
    assert t("no.such.key", "hi") == "no.such.key"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("hi-IN", "hi"), ("MR", "mr"), ("en_GB", "en"), ("fr", None), ("", None), (None, None)],
)
def test_normalise_language(raw: str | None, expected: str | None) -> None:
    assert normalise_language(raw) == expected
