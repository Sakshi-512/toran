from collections.abc import Callable

import httpx2
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.engine import Engine
from sqlmodel import Session, select

from app.config import get_settings
from app.models import Business, Enquiry

SLUG = "sunita-beauty-parlour"
ORIGIN = f"https://{SLUG}.example.test"
FORM = {
    "name": "Kavita",
    "phone": "90000 12345",
    "message": "Is Sunday free?",
    "lang": "hi",
    "website": "",
}


@pytest.fixture
def seeded(seed_sample: Callable[[str], Business]) -> Business:
    return seed_sample(SLUG)


def _enquiries(engine: Engine) -> list[Enquiry]:
    with Session(engine) as session:
        return list(session.exec(select(Enquiry)))


def _post(
    client: TestClient, origin: str | None, data: dict[str, str] | None = None, slug: str = SLUG
) -> httpx2.Response:
    headers = {"Origin": origin} if origin is not None else {}
    return client.post(
        f"/api/enquiry/{slug}", data=data or FORM, headers=headers, follow_redirects=False
    )


def test_valid_enquiry_saved_and_redirected(
    client: TestClient, seeded: Business, engine: Engine
) -> None:
    response = _post(client, ORIGIN)
    assert response.status_code == 303
    assert response.headers["location"] == f"{ORIGIN}/hi/thanks/"
    rows = _enquiries(engine)
    assert len(rows) == 1
    assert rows[0].business_id == seeded.id
    assert rows[0].phone == "+919000012345"  # full number kept for the owner to call back
    assert rows[0].message == "Is Sunday free?"


def test_development_origin_accepted_outside_production(
    client: TestClient, seeded: Business, engine: Engine
) -> None:
    response = _post(client, f"http://{SLUG}.localhost:8000", {**FORM, "lang": "en"})
    assert response.status_code == 303
    assert response.headers["location"] == f"http://{SLUG}.localhost:8000/thanks/"
    assert len(_enquiries(engine)) == 1


@pytest.mark.parametrize(
    "origin",
    [
        None,
        "null",
        "https://evil.example",
        f"http://{SLUG}.example.test",
        f"https://{SLUG}.example.test:8443",
        f"https://{SLUG}.example.test.evil.example",
        "https://meena-tailors.example.test",
        "https://example.test",
        f"https://x{SLUG}.example.test",
    ],
)
def test_wrong_origins_rejected(
    client: TestClient, seeded: Business, engine: Engine, origin: str | None
) -> None:
    response = _post(client, origin)
    assert response.status_code == 403
    assert _enquiries(engine) == []


def test_localhost_origin_rejected_in_production(
    monkeypatch: pytest.MonkeyPatch, seeded: Business, engine: Engine
) -> None:
    monkeypatch.setenv("APP_ENV", "production")
    get_settings.cache_clear()
    from app.main import create_app

    with TestClient(create_app(), base_url="https://example.test") as client:
        assert _post(client, f"http://{SLUG}.localhost:8000").status_code == 403
        assert _post(client, ORIGIN).status_code == 303
    assert len(_enquiries(engine)) == 1


def test_spam_trap_submission_not_saved(
    client: TestClient, seeded: Business, engine: Engine
) -> None:
    response = _post(client, ORIGIN, {**FORM, "website": "http://spam.example"})
    assert response.status_code == 303  # bots see success, nothing is stored
    assert _enquiries(engine) == []


def test_too_many_requests(client: TestClient, seeded: Business, engine: Engine) -> None:
    codes = [_post(client, ORIGIN).status_code for _ in range(6)]
    assert codes == [303] * 5 + [429]
    assert len(_enquiries(engine)) == 5


def test_unknown_business(client: TestClient, seeded: Business) -> None:
    assert _post(client, "https://nobody.example.test", slug="nobody").status_code == 404


@pytest.mark.parametrize(
    "bad",
    [
        {"name": ""},
        {"message": "  "},
        {"phone": "12345"},
        {"name": "x" * 121},
        {"message": "x" * 2001},
    ],
)
def test_invalid_input(
    client: TestClient, seeded: Business, engine: Engine, bad: dict[str, str]
) -> None:
    assert _post(client, ORIGIN, {**FORM, **bad}).status_code == 422
    assert _enquiries(engine) == []


def test_phone_is_optional(client: TestClient, seeded: Business, engine: Engine) -> None:
    assert _post(client, ORIGIN, {**FORM, "phone": ""}).status_code == 303
    assert _enquiries(engine)[0].phone is None


def test_enquiry_phone_redacted_in_logs(
    client: TestClient, seeded: Business, capsys: pytest.CaptureFixture[str]
) -> None:
    _post(client, ORIGIN)
    out = capsys.readouterr()
    assert "9000012345" not in out.out + out.err
    assert "90000 12345" not in out.out + out.err
