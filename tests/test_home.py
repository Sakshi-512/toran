"""Home page and /start redesign: honest numbers, real before and after, gallery consent,
one-tap samples, option cards, three languages and the page weight budget."""

import asyncio
import io
import json
import re
from datetime import timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from jinja2 import Environment
from PIL import Image
from sqlalchemy.engine import Engine
from sqlmodel import Session

from app.config import get_settings
from app.models import (
    AgentRun,
    Business,
    ClaudeUsage,
    IntakeSession,
    TestRun,
    utcnow,
)
from app.routes.intake import _first_file
from app.services import showcase
from app.services.i18n import SUPPORTED, t
from app.services.security import CSRF_FIELD
from tests.test_publish import _business

STATIC = "app/static"


def _usage(
    engine: Engine,
    purpose: str,
    cost: float,
    *,
    mock: bool,
    business_id: int | None = None,
    intake_id: int | None = None,
) -> None:
    with Session(engine) as session:
        session.add(
            ClaudeUsage(
                purpose=purpose,
                model="claude-sonnet-5-5",
                cost_usd_estimate=cost,
                is_mock=mock,
                day_ist=utcnow().date(),
                business_id=business_id,
                intake_id=intake_id,
            )
        )
        session.commit()


def _set(engine: Engine, business_id: int, **values: object) -> None:
    with Session(engine) as session:
        business = session.get(Business, business_id)
        assert business is not None
        for key, value in values.items():
            setattr(business, key, value)
        session.add(business)
        session.commit()


# --- honest numbers ---


def test_no_numbers_until_measured_and_mock_never_counts(
    client: TestClient, engine: Engine
) -> None:
    page = client.get("/").text
    assert 'class="stats"' not in page and "AI cost of one full check" not in page
    _usage(engine, "ai_checks", 0.05, mock=True)
    assert "AI cost of one full check" not in client.get("/").text
    _usage(engine, "ai_checks", 0.04, mock=False)
    _usage(engine, "ai_checks", 0.06, mock=False)
    page = client.get("/").text
    assert "AI cost of one full check" in page and "$0.05" in page
    assert "median of 2 real checks" in page


def _real_intake(engine: Engine, slug: str, guess_price: int, *, sample: bool = False) -> None:
    business_id, _, _ = _business(engine, slug=slug, is_sample=sample)

    def profile(price: int) -> dict[str, object]:
        product = {"name": "Cake", "unit": "kg", "options": [{"label": "", "price": price}]}
        return {"business_name": "Asha", "area": "Kothrud", "products": [product]}

    confirmed, guess = profile(450), profile(guess_price)
    with Session(engine) as session:
        intake = IntakeSession(
            source="voice",
            business_id=business_id,
            status="done",
            first_guess=guess,
            draft=confirmed,
            seconds_to_draft_scored=120,
        )
        session.add(intake)
        session.commit()
        assert intake.id is not None
        intake_id = intake.id
    _usage(engine, "extract", 0.01, mock=False, intake_id=intake_id)


def test_accuracy_shows_from_two_real_businesses_with_the_sample_size(
    client: TestClient, engine: Engine
) -> None:
    _real_intake(engine, "real-one", 450)
    assert "Details right first time" not in client.get("/").text  # only one so far
    _real_intake(engine, "a-sample", 450, sample=True)
    assert "Details right first time" not in client.get("/").text  # samples never count
    _real_intake(engine, "real-two", 999)
    page = client.get("/").text
    assert "Details right first time on real voice notes" in page
    assert "based on 2 real businesses" in page
    assert "From voice note to a tested draft" in page and "2.0 min" in page


# --- before and after, and the public panel ---


def _demo_run(engine: Engine, *, real: bool, slug: str = "savitri-gruh-udyog") -> str:
    business_id, _, _ = _business(engine, slug=slug, is_sample=True, source="seed")
    _set(engine, business_id, status="live", display_name="Savitri Gruh Udyog")
    started = utcnow() - timedelta(minutes=5)
    _usage(engine, "agent", 0.03, mock=not real, business_id=business_id)  # during the run
    with Session(engine) as session:
        run = AgentRun(
            id=f"ag_{slug}",
            business_id=business_id,
            status="done",
            stop_reason="no_improvement",
            rounds=2,
            score_before=85,
            score_after=87,
            created_at=started,
            finished_at=utcnow(),
        )
        session.add(run)
        session.commit()
    return f"ag_{slug}"


def test_before_and_after_is_hidden_without_a_real_run(client: TestClient, engine: Engine) -> None:
    assert "Before and after" not in client.get("/").text
    run_id = _demo_run(engine, real=False)
    assert "Before and after" not in client.get("/").text  # mock numbers are never shown
    assert client.get(f"/demo/agent/{run_id}").status_code == 404


def test_real_demo_run_shows_before_after_and_a_public_panel(
    client: TestClient, engine: Engine
) -> None:
    run_id = _demo_run(engine, real=True)
    page = client.get("/").text
    assert "Before and after" in page and "85/100" in page and "87/100" in page
    assert f'href="/demo/agent/{run_id}"' in page
    panel = client.get(f"/demo/agent/{run_id}?lang=mr")
    assert panel.status_code == 200 and "आधी" in panel.text and "डेमो व्यवसाय" in panel.text
    assert "/owner/publish" not in panel.text and "/undo" not in panel.text


def test_public_panel_never_shows_a_real_business(client: TestClient, engine: Engine) -> None:
    business_id, _, _ = _business(engine, slug="real-shop")
    with Session(engine) as session:
        session.add(AgentRun(id="ag_real", business_id=business_id, status="done"))
        session.commit()
    _usage(engine, "agent", 0.03, mock=False, business_id=business_id)
    assert client.get("/demo/agent/ag_real").status_code == 404


# --- gallery ---


def _live_with_shot(engine: Engine, business_id: int, total: int) -> None:
    settings = get_settings()
    settings.runs_dir.mkdir(parents=True, exist_ok=True)
    shot = settings.runs_dir / f"shot-{business_id}.png"
    Image.new("RGB", (360, 740), (200, 120, 80)).save(shot)
    with Session(engine) as session:
        session.add(
            TestRun(
                business_id=business_id,
                site_hash="h",
                url="u",
                site_kind="live",
                total=total,
                possible_so_far=100,
                screenshot_path=shot.name,
            )
        )
        session.commit()


def test_gallery_needs_her_separate_yes_and_samples_are_labelled(
    client: TestClient, engine: Engine
) -> None:
    real_id, _, _ = _business(engine, slug="meera-tiffins")
    _set(engine, real_id, status="live", display_name="Meera Tiffins", consent_given_at=utcnow())
    _live_with_shot(engine, real_id, 91)
    demo_id, _, _ = _business(engine, slug="demo-bakery", is_sample=True, source="seed")
    _set(engine, demo_id, status="live", display_name="Demo Bakery")
    page = client.get("/").text
    assert "Meera Tiffins" not in page  # live, but the owner has not said yes
    assert "Demo businesses" in page and "Demo Bakery" in page
    assert client.get("/gallery/meera-tiffins.webp").status_code == 404
    _set(engine, real_id, gallery_opt_in=True)
    page = client.get("/").text
    assert "Meera Tiffins" in page and "Readiness Score 91/100" in page
    assert page.index("Meera Tiffins") < page.index("Demo businesses")
    thumb = client.get("/gallery/meera-tiffins.webp")
    assert thumb.status_code == 200 and thumb.headers["content-type"] == "image/webp"
    assert Image.open(io.BytesIO(thumb.content)).width <= 240
    _set(engine, real_id, status="offline")
    assert "Meera Tiffins" not in client.get("/").text


def test_gallery_choice_on_her_owner_page(client: TestClient, engine: Engine) -> None:
    business_id, token, pin = _business(engine, slug="owner-shop")
    link = f"/o/{token}"
    csrf = re.search(rf'name="{CSRF_FIELD}" value="([^"]+)"', client.get(link).text).group(1)  # type: ignore[union-attr]
    client.post(link, data={CSRF_FIELD: csrf, "pin": pin})
    page = client.get("/owner").text
    assert "Show my site in the Toran gallery" in page and "Yes, show my site" in page
    client.post("/owner/gallery", data={CSRF_FIELD: csrf, "show": "yes"})
    with Session(engine) as session:
        assert session.get(Business, business_id).gallery_opt_in  # type: ignore[union-attr]
    client.post("/owner/gallery", data={CSRF_FIELD: csrf, "show": "no"})
    with Session(engine) as session:
        assert not session.get(Business, business_id).gallery_opt_in  # type: ignore[union-attr]


# --- trying it ---


def test_each_language_card_starts_the_real_flow(client: TestClient) -> None:
    page = client.get("/").text
    for sample in ("sample-en", "sample-hi", "sample-mr"):
        assert f'name="sample" value="{sample}"' in page
    csrf = re.search(rf'name="{CSRF_FIELD}" value="([^"]+)"', page).group(1)  # type: ignore[union-attr]
    started = client.post(
        "/start", data={CSRF_FIELD: csrf, "sample": "sample-mr"}, follow_redirects=False
    )
    assert started.status_code == 303 and started.headers["location"].endswith("/wait")
    assert 'href="/start?option=record#options"' in page


@pytest.mark.parametrize(
    ("lang", "headline", "try_title"),
    [
        ("en", "Speak your business. Get your website.", "Try it in 3 languages"),
        ("hi", "अपना व्यवसाय बोलिए। अपनी वेबसाइट पाइए।", "3 भाषाओं में आज़माइए"),
        ("mr", "तुमचा व्यवसाय बोला. तुमची वेबसाइट मिळवा.", "3 भाषांत वापरून पाहा"),
    ],
)
def test_home_in_three_languages(
    client: TestClient, lang: str, headline: str, try_title: str
) -> None:
    page = client.get(f"/?lang={lang}").text
    assert headline in page and try_title in page
    assert "GoDaddy Inclusive Innovation Hackathon 2026" in page and 'href="/privacy"' in page
    assert 'id="contact"' in page and "hero-sample.webp" in page and 'alt="' in page


def test_start_page_samples_first_then_four_cards(client: TestClient) -> None:
    page = client.get("/start").text
    assert page.index("Try a sample") < page.index("Or tell us about your business")
    for heading in (
        "Record",
        "Upload a voice note",
        "Photo of price list or rate card",
        "Type",
    ):
        assert page.count(f"<strong>{heading}</strong>") == 1
    assert page.count('type="radio" name="source"') == 4
    assert 'id="opt-record" checked' not in page
    assert 'id="opt-record" checked' in client.get("/start?option=record").text
    assert 'name="consent"' in page and 'name="keep_input"' in page and 'id="language"' in page


def test_the_first_non_empty_upload_is_used() -> None:
    from starlette.datastructures import Headers, UploadFile

    empty = UploadFile(io.BytesIO(b""), filename="", headers=Headers({}))
    photo = UploadFile(io.BytesIO(b"photo-bytes"), filename="p.jpg", headers=Headers({}))
    assert asyncio.run(_first_file([empty, photo])) == b"photo-bytes"
    assert asyncio.run(_first_file(["", empty])) == b""


# --- weight and accessibility basics ---


def test_home_page_stays_within_the_weight_budget(client: TestClient) -> None:
    html = client.get("/").content
    css = client.get("/static/app.css").content
    hero = client.get("/static/home/hero-sample.webp").content
    fonts = sum(
        len(client.get(f"/static/fonts/{name}.woff2").content)
        for name in ("mukta-400-latin", "mukta-700-latin", "rozha-400-latin")
    )
    total = len(html) + len(css) + len(hero) + fonts
    assert total < 500 * 1024, total
    assert len(hero) < 80 * 1024


def test_every_image_has_alt_text(client: TestClient, engine: Engine) -> None:
    demo_id, _, _ = _business(engine, slug="demo-bakery", is_sample=True, source="seed")
    _set(engine, demo_id, status="live")
    _live_with_shot(engine, demo_id, 88)
    for path in ("/", "/start"):
        page = client.get(path).text
        for tag in re.findall(r"<img [^>]*>", page):
            assert re.search(r'alt="[^"]+"', tag), tag


# --- headline highlight -------------------------------------------------------------------------


@pytest.mark.parametrize("lang", ["en", "hi", "mr"])
def test_headline_highlights_only_its_last_word(client: TestClient, lang: str) -> None:
    headline = t("home.headline", lang)
    h1 = client.get(f"/?lang={lang}").text.split("<h1>")[1].split("</h1>")[0]
    first, last = headline.rsplit(" ", 1)
    assert h1 == f'{first} <span class="hl">{last}</span>'
    assert re.sub(r"<[^>]+>", "", h1) == headline  # the words themselves are unchanged


@pytest.mark.parametrize(
    ("headline", "expected"), [("Toran", '<span class="hl">Toran</span>'), ("", "")]
)
def test_headline_highlight_handles_one_word_and_empty(headline: str, expected: str) -> None:
    source = (Path(__file__).parents[1] / "app/templates/app/home.html").read_text(encoding="utf-8")
    line = next(x for x in source.splitlines() if "set head =" in x)
    h1 = next(x for x in source.splitlines() if x.strip().startswith("<h1>"))
    template = Environment(autoescape=True).from_string(
        line.replace("t('home.headline', lang)", "headline") + h1
    )
    assert template.render(headline=headline).strip() == f"<h1>{expected}</h1>"


# --- facts and test results ---------------------------------------------------------------------

EVAL_OK = {
    "checks_passed": 77,
    "checks_total": 81,
    "cases": 12,
    "prices_exact": True,
    "run_date": "2026-11-03",
}


def _eval_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, content: str | None) -> None:
    path = tmp_path / "latest.json"
    if content is not None:
        path.write_text(content, encoding="utf-8")
    monkeypatch.setattr(showcase, "EVAL_SUMMARY", path)


def _facts(page: str) -> str:
    return page.split('class="facts-group"')[1].split("</section>")[0]


def test_facts_show_languages_and_eval_from_their_sources(
    client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _eval_file(tmp_path, monkeypatch, json.dumps(EVAL_OK))
    facts = _facts(client.get("/").text)
    assert "Facts and test results" in facts
    assert f"{len(SUPPORTED)} languages" in facts
    assert "77/81" in facts and "on 12 reviewed test cases, not live traffic" in facts
    assert "last run 03/11/2026" in facts
    assert "You say yes first" in facts and "until you have checked every detail" in facts
    assert facts.count('<div class="stat">') == 3


@pytest.mark.parametrize(
    "content",
    [
        None,
        "not json",
        json.dumps({**EVAL_OK, "checks_passed": 90}),
        json.dumps({**EVAL_OK, "run_date": "yesterday"}),
        json.dumps({k: v for k, v in EVAL_OK.items() if k != "cases"}),
    ],
)
def test_missing_or_bad_eval_summary_hides_only_that_figure(
    client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, content: str | None
) -> None:
    _eval_file(tmp_path, monkeypatch, content)
    facts = _facts(client.get("/").text)
    assert f"{len(SUPPORTED)} languages" in facts
    assert "Extraction checks passed" not in facts
    assert "You say yes first" in facts and facts.count('<div class="stat">') == 2


def test_without_live_numbers_only_the_facts_group_shows(
    client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _eval_file(tmp_path, monkeypatch, json.dumps(EVAL_OK))
    page = client.get("/").text
    assert "Measured on the live system" not in page and "Real numbers from real use" not in page
    assert '<h2 id="facts-title">Facts and test results</h2>' in page
    assert 'aria-labelledby="facts-title"' in page


def test_with_live_numbers_both_groups_show_live_first(
    client: TestClient, engine: Engine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _eval_file(tmp_path, monkeypatch, json.dumps(EVAL_OK))
    _usage(engine, "ai_checks", 0.04, mock=False)
    page = client.get("/").text
    assert page.index("Measured on the live system") < page.index("Facts and test results")
    assert '<h3 id="facts-title">' in page


def test_no_figure_is_hard_coded_in_the_template() -> None:
    source = (Path(__file__).parents[1] / "app/templates/app/home.html").read_text("utf-8")
    strip = source.split('class="home-section home-band measured proof-band"')[1].split(
        "</section>"
    )[0]
    allowed = r"'%\.\d+f'|\* 100|== 1\b|'%d/%m/%Y'|\bh[23]\b"  # formats, plural switch, tags
    assert not re.search(r"\d", re.sub(allowed, "", strip))


def test_committed_eval_summary_is_valid() -> None:
    summary = showcase.load_eval_summary()
    assert summary is not None and summary.checks_passed <= summary.checks_total


@pytest.mark.parametrize("lang", ["en", "hi", "mr"])
def test_the_yes_first_fact_in_three_languages(client: TestClient, lang: str) -> None:
    facts = _facts(client.get(f"/?lang={lang}").text)
    assert t("home.stat_yes", lang) in facts and t("home.stat_yes_note", lang) in facts
