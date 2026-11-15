from collections.abc import Callable

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.engine import Engine
from sqlmodel import Session, select

from app.config import get_settings
from app.models import Business, utcnow

SLUG = "asha-home-bakery"
HOST = f"{SLUG}.example.test"


@pytest.fixture
def seeded(seed_sample: Callable[[str], Business]) -> Business:
    return seed_sample(SLUG)


def test_site_served_on_subdomain(client: TestClient, seeded: Business) -> None:
    for path, lang in (("/", "en"), ("/hi/", "hi"), ("/mr/", "mr"), ("/hi/thanks/", "hi")):
        response = client.get(path, headers={"Host": HOST})
        assert response.status_code == 200, path
        assert f'<html lang="{lang}">' in response.text
        assert "Asha&#39;s Home Bakery" in response.text


def test_site_security_headers(client: TestClient, seeded: Business) -> None:
    headers = client.get("/", headers={"Host": HOST}).headers
    csp = headers["content-security-policy"]
    assert "default-src 'none'" in csp
    assert "script-src" not in csp
    assert "form-action http://localhost:8000" in csp
    assert "frame-ancestors 'none'" in csp
    assert headers["x-content-type-options"] == "nosniff"
    assert headers["referrer-policy"] == "strict-origin-when-cross-origin"
    assert headers["cache-control"] == "no-cache"


def test_assets_cached_forever(client: TestClient, seeded: Business) -> None:
    html = client.get("/", headers={"Host": HOST}).text
    css_href = html.split('rel="stylesheet" href="')[1].split('"')[0]
    response = client.get("/" + css_href, headers={"Host": HOST})
    assert response.status_code == 200
    assert "immutable" in response.headers["cache-control"]
    assert response.headers["content-type"].startswith("text/css")


def test_localhost_preview_in_development(client: TestClient, seeded: Business) -> None:
    response = client.get("/", headers={"Host": f"{SLUG}.localhost:8000"})
    assert response.status_code == 200


def test_unknown_and_deleted_sites_404(
    client: TestClient, seeded: Business, engine: Engine
) -> None:
    assert client.get("/", headers={"Host": "nobody.example.test"}).status_code == 404
    with Session(engine) as session:
        business = session.exec(select(Business).where(Business.slug == SLUG)).one()
        business.deleted_at = utcnow()
        session.add(business)
        session.commit()
    assert client.get("/", headers={"Host": HOST}).status_code == 404


def test_path_traversal_blocked(client: TestClient, seeded: Business) -> None:
    for path in ("/../../app.db", "/%2e%2e/%2e%2e/test.db", "/assets/../../live/index.html/.."):
        assert client.get(path, headers={"Host": HOST}).status_code == 404


def test_site_only_accepts_get(client: TestClient, seeded: Business) -> None:
    assert client.post("/", headers={"Host": HOST}).status_code == 405


def test_main_domain_still_serves_app(client: TestClient, seeded: Business) -> None:
    response = client.get("/", headers={"Host": "example.test"})
    assert "Test App" in response.text
    assert client.get("/", headers={"Host": "www.example.test"}).status_code == 200


def test_dev_theme_gallery(client: TestClient) -> None:
    response = client.get("/dev/themes")
    assert response.status_code == 200
    from app.services.samples import load_samples

    assert response.text.count("<iframe") == len(load_samples()) * 3
    preview = client.get("/dev/preview/meena-tailors/elegant/hi/")
    assert preview.status_code == 200
    assert '<html lang="hi">' in preview.text
    assert "frame-ancestors 'self'" in preview.headers["content-security-policy"]
    assert preview.headers["x-frame-options"] == "SAMEORIGIN"
    assert client.get("/dev/preview/nobody/warm/").status_code == 404


def test_privacy_page_in_three_languages(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CONTACT_TO_EMAIL", "hello@example.test")
    get_settings.cache_clear()
    english = client.get("/privacy?lang=en").text
    for phrase in (
        "90 days",
        "30 days",
        "7 days",
        "deleted right after processing",
        "hello@example.test",
    ):
        assert phrase in english
    assert "Application logs: kept for 30 days." in english
    assert "about 2 MB, kept by size and overwritten automatically" in english
    assert (
        "Both logs are redacted and never contain recordings, secrets or full phone numbers."
        in english
    )
    assert "Logs: kept for 30 days" not in english
    hindi = client.get("/privacy?lang=hi").text
    assert "90 दिन" in hindi and "ऐप्लिकेशन लॉग: 30 दिन" in hindi and "2 MB" in hindi
    assert "दोनों लॉग" in hindi
    marathi = client.get("/privacy?lang=mr").text
    assert "90 दिवस" in marathi and "ॲप्लिकेशन लॉग: 30 दिवस" in marathi and "2 MB" in marathi
    assert "दोन्ही लॉग" in marathi


def test_production_hides_dev_routes_and_localhost(
    monkeypatch: pytest.MonkeyPatch, seeded: Business
) -> None:
    monkeypatch.setenv("APP_ENV", "production")
    get_settings.cache_clear()
    from app.main import create_app

    with TestClient(create_app(), base_url="https://example.test") as client:
        assert client.get("/dev/themes").status_code == 404
        assert client.get("/", headers={"Host": f"{SLUG}.localhost:8000"}).status_code == 200
        response = client.get("/", headers={"Host": f"{SLUG}.localhost:8000"})
        assert "Test App" in response.text  # main app, not the business site


def test_error_pages_on_subdomain_are_styled(client: TestClient, seeded: Business) -> None:
    response = client.get("/no-such-page", headers={"Host": HOST, "Accept-Language": "mr"})
    assert response.status_code == 404
    assert "हे पान सापडलं नाही" in response.text
    assert 'href="/static/app.css"' in response.text
    css = client.get("/static/app.css", headers={"Host": HOST})
    assert css.status_code == 200
    assert css.headers["content-type"].startswith("text/css")
