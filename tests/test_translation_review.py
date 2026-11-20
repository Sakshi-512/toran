"""Translations: identical keys and placeholders in every language, the review sheet, applying a
native speaker's corrections, the measured-figures block and the enquiry note."""

import json
from datetime import date

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.engine import Engine

from app.services.i18n import I18N_DIR, SUPPORTED
from scripts import i18n_review as review
from tests.test_home import _usage


def test_all_three_files_have_identical_keys() -> None:
    keys = [set(json.loads((I18N_DIR / f"{c}.json").read_text("utf-8"))) for c in SUPPORTED]
    assert keys[0] == keys[1] == keys[2]


def test_placeholders_and_numbers_match_the_english() -> None:
    assert review.problems(review.load()) == []


def test_no_gendered_owner_wording_in_english() -> None:
    import re

    english = review.load()["en"]
    found = [
        k for k, v in english.items() if re.search(r"\b(she|her)\b", v, re.I) and "Asha" not in v
    ]
    assert found == []


def test_sheet_lists_every_key_in_screen_order_with_a_fix_column() -> None:
    data = review.load()
    sheet = review.sheet(data, ["privacy.profile"])
    assert "| Key | English | Hindi | Marathi | Fix |" in sheet
    assert sheet.index("## Needs a human read") < sheet.index("## Home page")
    assert (
        sheet.index("## Home page") < sheet.index("## Start page") < sheet.index("## Privacy page")
    )
    for key in data["en"]:
        assert f"`{key}`" in sheet


def test_corrections_round_trip_and_bad_placeholders_are_refused() -> None:
    data = review.load()
    filled = review.sheet(data, []).replace("| `home.try_now` |", "| `home.try_now` |", 1)
    row = next(line for line in filled.splitlines() if line.startswith("| `home.try_now` |"))
    filled = filled.replace(row, row[:-2] + "hi: अभी आज़माकर देखिए || mr: आत्ता करून पाहा |")
    bad_row = next(line for line in filled.splitlines() if line.startswith("| `home.ba_intro` |"))
    filled = filled.replace(bad_row, bad_row[:-2] + "hi: डेमो ड्राफ्ट बेहतर किया |")
    fixes = review.corrections(filled)
    assert ("home.try_now", "hi", "अभी आज़माकर देखिए") in fixes
    assert ("home.try_now", "mr", "आत्ता करून पाहा") in fixes
    refused = review.apply(data, fixes)
    assert data["hi"]["home.try_now"] == "अभी आज़माकर देखिए"
    assert any("home.ba_intro" in r and "placeholders" in r for r in refused)
    assert "{name}" in data["hi"]["home.ba_intro"]  # the broken fix was not applied


def test_measured_block_follows_the_hero_with_sample_size_and_date(
    client: TestClient, engine: Engine
) -> None:
    _usage(engine, "ai_checks", 0.04, mock=False)
    page = client.get("/").text
    assert page.index('class="home-hero"') < page.index("Measured on the live system")
    assert page.index("Measured on the live system") < page.index("How it works")
    assert page.index("Measured on the live system") < page.index('id="contact"')
    how = page.split("How it works")[1].split("</section>")[0]
    assert "AI cost of one full check" not in how
    assert "from 1 check, which may be a sample business ·" in page and "median of" not in page
    from datetime import datetime

    from app.services.showcase import IST

    assert f"last measured {datetime.now(IST).strftime('%d/%m/%Y')}" in page


def test_no_measured_block_without_real_numbers(client: TestClient) -> None:
    assert "Measured on the live system" not in client.get("/").text


@pytest.mark.parametrize(
    ("path", "text"),
    [
        ("index.html", "Your message is sent to the business through Test"),
        ("hi/index.html", "के ज़रिए इस व्यवसाय तक पहुँचता है"),
        ("mr/index.html", "मार्फत या व्यवसायाला पाठवला जातो"),
    ],
)
def test_enquiry_note_under_the_form(path: str, text: str) -> None:
    from app.services.sitegen import SiteInput, build_site
    from tests.test_agent import PROFILE

    site = SiteInput(
        slug="x",
        display_name="x",
        profile=PROFILE,
        theme="warm",
        is_sample=False,
        prices_updated=date(2026, 11, 1),
        site_url="https://x.example.test",
        main_url="https://example.test",
        enquiry_url="https://example.test/api/enquiry/x",
        app_name="Test",
    )
    html = build_site(site).files[path].decode()
    form = html.split("<!--enquiry-form-->")[1].split("<!--/enquiry-form-->")[0]
    assert text in form
