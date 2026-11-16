"""Polish pass: status text, the phone rule, sample order buttons, My details, how-to-fix."""

import re
from datetime import date

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.engine import Engine
from sqlmodel import Session, select

from app.config import get_settings
from app.models import Business, Job, ProfileVersion, ScoreItem, TestRun
from app.schemas import BusinessProfile
from app.services import publish as pub
from app.services.errors import AppError
from app.services.samples import get_sample
from app.services.security import CSRF_FIELD
from app.services.sitegen import SiteInput, build_site
from tests.test_publish import _business


def _csrf(client: TestClient, path: str) -> str:
    return re.search(rf'name="{CSRF_FIELD}" value="([^"]+)"', client.get(path).text).group(1)  # type: ignore[union-attr]


def _signed_in(client: TestClient, engine: Engine, **kwargs: object) -> int:
    business_id, token, pin = _business(engine, **kwargs)  # type: ignore[arg-type]
    link = f"/o/{token}"
    client.post(link, data={CSRF_FIELD: _csrf(client, link), "pin": pin})
    return business_id


def _set_profile(engine: Engine, business_id: int, **changes: object) -> None:
    with Session(engine) as session:
        version = session.exec(
            select(ProfileVersion).where(ProfileVersion.business_id == business_id)
        ).one()
        version.profile = {**version.profile, **changes}
        session.add(version)
        session.commit()


# --- owner page status and actions ---


def test_status_matches_the_state(client: TestClient, engine: Engine) -> None:
    business_id = _signed_in(client, engine)
    assert "being prepared" in client.get("/owner").text  # no draft yet
    pub.build_draft(engine, get_settings(), business_id)
    with Session(engine) as session:
        session.add(
            TestRun(business_id=business_id, site_hash="h", url="u", site_kind="draft", total=80)
        )
        session.commit()
    page = client.get("/owner").text
    assert "Your draft is ready to review. Nothing is live yet." in page
    assert "being prepared" not in page
    primary = page.index('class="primary-action"')
    assert page.index("Make my site live") > primary
    assert page.index("Make my site live") < page.index('class="action-grid"')


# --- the phone rule ---


def test_publishing_needs_a_phone_or_her_choice(client: TestClient, engine: Engine) -> None:
    business_id = _signed_in(client, engine)
    _set_profile(engine, business_id, phone="")
    pub.build_draft(engine, get_settings(), business_id)
    page = client.get("/owner/publish").text
    assert "customers can&#39;t order" in page or "customers can't order" in page
    assert 'action="/owner/publish"' not in page and 'href="/owner/details#phone"' in page
    with pytest.raises(AppError, match=r"phone\.required"):
        pub.publish(engine, get_settings(), business_id)
    csrf = _csrf(client, "/owner/details")
    client.post(
        "/owner/details",
        data={
            CSRF_FIELD: csrf,
            "phone": "",
            "area": "Kothrud",
            "policy": "",
            "no_order_button": "yes",
        },
    )
    with Session(engine) as session:
        latest = pub.latest_profile(session, business_id)
        assert latest is not None and latest.profile["order_methods"] == []
    assert 'action="/owner/publish"' in client.get("/owner/publish").text
    pub.publish(engine, get_settings(), business_id)  # her explicit choice: allowed


def test_adding_a_phone_unblocks_publishing(client: TestClient, engine: Engine) -> None:
    business_id = _signed_in(client, engine)
    _set_profile(engine, business_id, phone="")
    csrf = _csrf(client, "/owner/details")
    client.post(
        "/owner/details",
        data={CSRF_FIELD: csrf, "phone": "98220 12345", "area": "Kothrud", "policy": ""},
    )
    pub.publish(engine, get_settings(), business_id)
    with Session(engine) as session:
        assert session.get(Business, business_id).status == "live"  # type: ignore[union-attr]


def test_sample_sites_always_have_an_order_button_but_never_a_number() -> None:
    sample = get_sample("asha-home-bakery")
    assert sample is not None
    profile = sample.profile.model_copy(update={"phone": ""})
    site = SiteInput(
        slug="x",
        display_name="x",
        profile=profile,
        theme="warm",
        is_sample=True,
        prices_updated=date(2026, 11, 1),
        site_url="https://x.example.test",
        main_url="https://example.test",
        enquiry_url="https://example.test/api/enquiry/x",
        app_name="Test",
    )
    for path in ("index.html", "hi/index.html", "mr/index.html"):
        html = build_site(site).files[path].decode()
        assert 'class="btn btn-main" href="#sample-note"' in html and 'id="sample-note"' in html
        assert "tel:" not in html and "wa.me" not in html
        assert 'class="steps"' in html
    real = build_site(site.__class__(**{**site.__dict__, "is_sample": False})).files["index.html"]
    assert b"btn-main" not in real.split(b'<section class="enquiry"')[0]


def test_confirm_page_warns_without_a_phone(client: TestClient, engine: Engine) -> None:
    from tests.test_intake_flow import TEXT, _start

    token = _start(client, engine, source="text", text=TEXT)
    page = client.get(f"/start/{token}/confirm").text
    fields = {
        k: v
        for k, v in re.findall(r'name="([^"]+)" value="([^"]*)"', page)
        if not k.endswith("_remove") and k not in ("languages", "order_methods", "action")
    }
    fields.update(phone="", action="save", order_methods="whatsapp")
    page = client.post(f"/start/{token}/confirm", data=fields).text
    assert "Without a number, customers can" in page and 'name="no_order_button"' in page
    fields.update(no_order_button="yes")
    page = client.post(f"/start/{token}/confirm", data=fields).text
    assert 'name="no_order_button" value="yes" checked' in page


# --- My details ---


def test_my_details_saves_a_new_version_and_retests(client: TestClient, engine: Engine) -> None:
    business_id = _signed_in(client, engine)
    pub.build_draft(engine, get_settings(), business_id)
    page = client.get("/owner/details").text
    assert 'id="phone"' in page and 'id="area"' in page and 'id="policy"' in page
    assert 'name="products' not in page and "price" not in page.lower().split("<form")[1].split(
        "</form>"
    )[0].replace("prices", "")
    csrf = _csrf(client, "/owner/details")
    client.post(
        "/owner/details",
        data={
            CSRF_FIELD: csrf,
            "phone": "+91 98220 12345",
            "area": "Karve Nagar",
            "policy": "50% advance.",
        },
    )
    with Session(engine) as session:
        versions = session.exec(
            select(ProfileVersion).where(ProfileVersion.business_id == business_id)
        ).all()
        jobs = session.exec(select(Job).where(Job.kind == "score")).all()
    latest = BusinessProfile.model_validate(versions[-1].profile)
    assert len(versions) == 2 and latest.area == "Karve Nagar" and latest.policy == "50% advance."
    assert latest.products == BusinessProfile.model_validate(versions[0].profile).products
    assert jobs and jobs[-1].payload == {"kind": "draft"}
    with Session(engine) as session:
        slug = session.get(Business, business_id).slug  # type: ignore[union-attr]
    draft = pub.folders(get_settings(), slug).draft / "index.html"
    assert "50% advance." in draft.read_text()


@pytest.mark.parametrize(
    ("field", "value", "error"),
    [
        ("area", "Flat 12, 4th floor, Shanti Apartments", "looks like an address"),
        ("phone", "12", "phone"),
    ],
)
def test_my_details_refuses_bad_input(
    client: TestClient, engine: Engine, field: str, value: str, error: str
) -> None:
    _signed_in(client, engine)
    data = {
        CSRF_FIELD: _csrf(client, "/owner/details"),
        "phone": "",
        "area": "Kothrud",
        "policy": "",
    }
    data[field] = value
    page = client.post("/owner/details", data=data)
    assert page.status_code == 422 and error in page.text.lower()


# --- how to fix ---


@pytest.mark.parametrize(
    ("lang", "how", "link"),
    [
        ("en", "How to fix:", "Change my details"),
        ("hi", "कैसे ठीक करें:", "मेरी जानकारी बदलें"),
        ("mr", "कसे दुरुस्त करायचे:", "माझी माहिती बदला"),
    ],
)
def test_report_says_how_to_fix_with_a_link(
    client: TestClient, engine: Engine, lang: str, how: str, link: str
) -> None:
    business_id = _signed_in(client, engine)
    with Session(engine) as session:
        run = TestRun(business_id=business_id, site_hash="h", url="u", site_kind="draft", total=60)
        session.add(run)
        session.commit()
        assert run.id is not None
        for key, part in (("phone_visible", "trust"), ("https", "trust")):
            session.add(
                ScoreItem(
                    run_id=run.id,
                    part=part,
                    key=key,
                    source="measured",
                    status="scored",
                    points=0,
                    max_points=5,
                    reason_en="x",
                    reason_hi="x",
                    reason_mr="x",
                )
            )
        session.commit()
        run_id = run.id
    page = client.get(f"/owner/score/{run_id}?lang={lang}").text
    assert page.count(how) == 2 and link in page and 'href="/owner/details#phone"' in page
    https_item = page.split("score-item")[-1]
    assert 'href="/owner' not in https_item.split("</li>")[0]  # nothing she can do herself


# --- home cards ---


def test_try_cards_have_the_button_last(client: TestClient) -> None:
    page = client.get("/").text
    cards = re.findall(r'<form class="try-card".*?</form>', page, re.S)
    assert len(cards) == 3 and all(c.rstrip("</form>").strip().endswith("</button>") for c in cards)
