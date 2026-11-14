import secrets
from collections.abc import Iterator

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.services.errors import AppError, SpendCapReached

LEAKED_VALUE = secrets.token_urlsafe(12)


@pytest.fixture
def error_client(app: FastAPI) -> Iterator[TestClient]:
    @app.get("/_test/boom")
    def boom() -> None:
        raise RuntimeError(f"database password is {LEAKED_VALUE}")

    @app.get("/_test/cap")
    def cap() -> None:
        raise SpendCapReached(5.1, 5.0)

    @app.get("/_test/app-error")
    def app_error() -> None:
        raise AppError("upload.too_large")

    @app.get("/_test/number/{n}")
    def number(n: int) -> dict[str, int]:
        return {"n": n}

    with TestClient(app, raise_server_exceptions=False) as client:
        yield client


def test_unhandled_error_is_friendly_and_hides_details(error_client: TestClient) -> None:
    response = error_client.get("/_test/boom")
    assert response.status_code == 500
    assert "Traceback" not in response.text
    assert LEAKED_VALUE not in response.text
    assert "RuntimeError" not in response.text
    assert "try again" in response.text
    assert response.headers["content-security-policy"]


def test_unhandled_error_in_owner_language(error_client: TestClient) -> None:
    response = error_client.get("/_test/boom", headers={"Accept-Language": "mr"})
    assert "पुन्हा प्रयत्न करा" in response.text


def test_not_found_in_hindi(error_client: TestClient) -> None:
    response = error_client.get("/nope?lang=hi")
    assert response.status_code == 404
    assert "यह पेज नहीं मिला" in response.text


def test_spend_cap_message(error_client: TestClient) -> None:
    response = error_client.get("/_test/cap")
    assert response.status_code == 503
    assert "Your site stays live" in response.text


def test_app_error_and_validation(error_client: TestClient) -> None:
    assert "too large" in error_client.get("/_test/app-error").text
    response = error_client.get("/_test/number/abc")
    assert response.status_code == 422
    assert "check them" in response.text


def test_json_callers_get_json(error_client: TestClient) -> None:
    response = error_client.get("/_test/cap", headers={"Accept": "application/json"})
    assert response.json()["error"] == "error.spend_cap"
