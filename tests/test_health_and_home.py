from fastapi.testclient import TestClient
from sqlalchemy.engine import Engine
from sqlmodel import Session

from app.models import Business, utcnow


def test_health(client: TestClient) -> None:
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_home_uses_settings(client: TestClient) -> None:
    response = client.get("/")
    assert response.status_code == 200
    assert "Test App" in response.text
    assert "Get a live website with a readiness score." in response.text


def test_home_in_marathi_and_hindi(client: TestClient) -> None:
    assert "आत्ता वापरून पाहा" in client.get("/?lang=mr").text
    assert "अभी आज़माइए" in client.get("/?lang=hi").text
    # The choice is remembered in the session.
    assert "आज़माइए" in client.get("/").text


def test_home_from_accept_language(client: TestClient) -> None:
    response = client.get("/", headers={"Accept-Language": "mr-IN,en;q=0.5"})
    assert 'lang="mr"' in response.text


def _add_business(engine: Engine, slug: str, deleted: bool = False) -> None:
    with Session(engine) as session:
        session.add(
            Business(slug=slug, display_name=slug, deleted_at=utcnow() if deleted else None)
        )
        session.commit()


def test_tls_check_allows_main_domain_and_real_businesses(
    client: TestClient, engine: Engine
) -> None:
    _add_business(engine, "sunita-cakes")
    _add_business(engine, "gone", deleted=True)
    assert client.get("/internal/tls-check?domain=example.test").status_code == 200
    assert client.get("/internal/tls-check?domain=www.example.test").status_code == 200
    assert client.get("/internal/tls-check?domain=sunita-cakes.example.test").status_code == 200
    assert client.get("/internal/tls-check?domain=gone.example.test").status_code == 404
    assert client.get("/internal/tls-check?domain=nobody.example.test").status_code == 404
    assert client.get("/internal/tls-check?domain=a.b.example.test").status_code == 404
    assert client.get("/internal/tls-check?domain=evil.com").status_code == 404


def test_tls_check_refuses_proxied_requests(client: TestClient) -> None:
    response = client.get(
        "/internal/tls-check?domain=example.test", headers={"X-Forwarded-For": "1.2.3.4"}
    )
    assert response.status_code == 404
