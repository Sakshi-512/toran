from collections.abc import Iterator

import pytest
from fastapi import APIRouter, Depends, FastAPI, Request
from fastapi.testclient import TestClient

from app.config import get_settings
from app.services.ratelimit import FORM_LIMIT, rate_limit
from app.services.security import CSRF_FIELD, csrf_token, is_spam, verify_csrf


def _add_test_routes(app: FastAPI) -> None:
    router = APIRouter()

    @router.get("/_test/form")
    def form(request: Request) -> dict[str, str]:
        return {"token": csrf_token(request)}

    @router.post(
        "/_test/form", dependencies=[Depends(rate_limit(FORM_LIMIT)), Depends(verify_csrf)]
    )
    def submit(request: Request) -> dict[str, str]:
        return {"ok": "yes"}

    @router.post("/_test/open")
    def open_form(request: Request) -> dict[str, str]:
        return {"ok": "yes"}

    app.include_router(router)


@pytest.fixture
def form_client(app: FastAPI) -> Iterator[TestClient]:
    _add_test_routes(app)
    with TestClient(app, raise_server_exceptions=False) as client:
        yield client


def test_security_headers_on_every_response(client: TestClient) -> None:
    for path in ("/", "/health", "/missing"):
        headers = client.get(path).headers
        assert "default-src 'self'" in headers["content-security-policy"]
        assert "frame-ancestors 'none'" in headers["content-security-policy"]
        assert headers["x-content-type-options"] == "nosniff"
        assert headers["referrer-policy"] == "strict-origin-when-cross-origin"
        assert len(headers["x-request-id"]) == 32


def test_session_cookie_flags_development(client: TestClient) -> None:
    cookie = client.get("/?lang=hi").headers["set-cookie"].lower()
    assert "httponly" in cookie
    assert "samesite=lax" in cookie
    assert "secure" not in cookie


def test_session_cookie_secure_in_production(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APP_ENV", "production")
    get_settings.cache_clear()
    from app.main import create_app

    with TestClient(create_app(), base_url="https://example.test") as client:
        response = client.get("/?lang=hi")
    cookie = response.headers["set-cookie"].lower()
    assert "secure" in cookie and "httponly" in cookie
    assert "max-age=31536000" in response.headers["strict-transport-security"]


def test_csrf_valid_token_accepted(form_client: TestClient) -> None:
    token = form_client.get("/_test/form").json()["token"]
    response = form_client.post("/_test/form", data={CSRF_FIELD: token})
    assert response.status_code == 200


def test_csrf_missing_or_wrong_token_rejected(form_client: TestClient) -> None:
    form_client.get("/_test/form")
    assert form_client.post("/_test/form", data={}).status_code == 403
    assert form_client.post("/_test/form", data={CSRF_FIELD: "forged"}).status_code == 403


def test_csrf_token_from_other_session_rejected(app: FastAPI, form_client: TestClient) -> None:
    token = form_client.get("/_test/form").json()["token"]
    with TestClient(app) as other:
        other.get("/_test/form")
        assert other.post("/_test/form", data={CSRF_FIELD: token}).status_code == 403


def test_form_rate_limit(form_client: TestClient) -> None:
    token = form_client.get("/_test/form").json()["token"]
    codes = [
        form_client.post("/_test/form", data={CSRF_FIELD: token}).status_code for _ in range(6)
    ]
    assert codes[:5] == [200] * 5
    assert codes[5] == 429


def test_default_rate_limit_on_public_pages(client: TestClient) -> None:
    codes = [client.get("/").status_code for _ in range(61)]
    assert codes[-1] == 429
    page = client.get("/", headers={"Accept-Language": "hi"})
    assert "एक मिनट" in page.text


def test_health_is_not_rate_limited(client: TestClient) -> None:
    assert all(client.get("/health").status_code == 200 for _ in range(70))


def test_spam_trap() -> None:
    assert is_spam({"website": "http://spam"}) is True
    assert is_spam({"website": ""}) is False
    assert is_spam({}) is False
