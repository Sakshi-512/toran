import json
import re
import shutil
from datetime import timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.engine import Engine
from sqlmodel import Session, select

from app.config import get_settings
from app.models import Business, IntakeSession, Photo, utcnow
from app.schemas import BusinessProfile
from app.services.auth import issue_access
from app.services.evals import ROOT, load_cases, render_report, run_evals, score_case
from app.services.retention import purge_stale_inputs
from app.services.security import CSRF_FIELD

CASES = ROOT / "cases"

# --- evals ------------------------------------------------------------------------------------


def test_committed_cases_are_fake_and_mostly_reviewed() -> None:
    cases = load_cases((CASES,))
    assert len(cases) >= 10
    assert all(not private for _, private in cases)
    assert [c["id"] for c, _ in cases if not c["reviewed"]] == ["draft-unreviewed"]


def test_mock_eval_scores_every_reviewed_case(engine: Engine) -> None:
    report = run_evals(engine, get_settings(), (CASES,))
    totals = report.totals()
    assert totals["cases"] == 11 and report.skipped == ["draft-unreviewed"]
    assert totals["accuracy"] == 1.0 and totals["price_accuracy"] == 1.0
    text = render_report(report)
    assert "Product-name decisions by Claude (please check)" in text
    assert '"Ukadiche modak" vs "उकडीचे मोदक"' in text
    assert "draft-unreviewed" in text


def test_private_cases_are_included_and_labelled(engine: Engine, tmp_path: Path) -> None:
    private = tmp_path / "private"
    private.mkdir()
    shutil.copy(CASES / "typed-priya.json", private / "typed-priya.json")
    report = run_evals(engine, get_settings(), (private,))
    assert report.results[0].private
    assert "typed-priya (private)" in render_report(report)


def _profile(**changes: object) -> BusinessProfile:
    data: dict[str, object] = {
        "business_type": "bakery",
        "phone": "9876543210",
        "products": [
            {"name": "Chocolate cake", "unit": "kg", "options": [{"label": "1 kg", "price": 1250}]}
        ],
    }
    data.update(changes)
    return BusinessProfile.model_validate(data)


CASE = {
    "id": "c",
    "expected": {
        "business_type": "bakery",
        "phone": "9876543210",
        "products": [{"name": "Chocolate cake", "options": [{"price": 1250}]}],
    },
}


def test_prices_checked_exactly(engine: Engine) -> None:
    from app.services.claude_client import ClaudeClient

    client = ClaudeClient(get_settings(), engine)
    wrong = _profile(
        products=[
            {"name": "Chocolate cake", "unit": "kg", "options": [{"label": "1 kg", "price": 1200}]}
        ]
    )
    result = score_case(CASE, wrong, "Chocolate cake 1250", stt_used=False, client=client)
    failed = [c for c in result.checks if not c.ok]
    assert [c.name for c in failed] == ["products[Chocolate cake].options[].price"]
    assert failed[0].cause == "Claude"


def test_cause_is_speech_to_text_when_transcript_was_wrong(engine: Engine) -> None:
    from app.services.claude_client import ClaudeClient

    client = ClaudeClient(get_settings(), engine)
    wrong = _profile(
        products=[
            {"name": "Chocolate cake", "unit": "kg", "options": [{"label": "1 kg", "price": 1200}]}
        ]
    )
    heard = "Chocolate cake 1200 rupees"  # speech-to-text misheard 1250
    result = score_case(CASE, wrong, heard, stt_used=True, client=client)
    assert [c.cause for c in result.checks if not c.ok] == ["speech-to-text"]
    right = score_case(CASE, wrong, "Chocolate cake 1250 rupees", stt_used=True, client=client)
    assert [c.cause for c in right.checks if not c.ok] == ["Claude"]


# --- owner photos -------------------------------------------------------------------------------


def _jpeg(color: tuple[int, int, int] = (200, 120, 60)) -> bytes:
    import io

    from PIL import Image

    out = io.BytesIO()
    Image.new("RGB", (64, 48), color).save(out, format="JPEG")
    return out.getvalue()


@pytest.fixture
def signed_in(client: TestClient, engine: Engine) -> int:
    with Session(engine) as session:
        business = Business(slug="shop", display_name="Shop")
        access = issue_access(business)
        session.add(business)
        session.commit()
        business_id = business.id
    page = client.get(f"/o/{access.link_token}").text
    token = re.search(rf'name="{CSRF_FIELD}" value="([^"]+)"', page).group(1)  # type: ignore[union-attr]
    client.post(f"/o/{access.link_token}", data={CSRF_FIELD: token, "pin": access.pin})
    assert business_id is not None
    return business_id


def _csrf(client: TestClient) -> str:
    return re.search(rf'name="{CSRF_FIELD}" value="([^"]+)"', client.get("/owner").text).group(1)  # type: ignore[union-attr]


def test_owner_adds_and_manages_photos(client: TestClient, engine: Engine, signed_in: int) -> None:
    files = [
        ("photos", ("a.jpg", _jpeg(), "image/jpeg")),
        ("photos", ("b.jpg", _jpeg((10, 10, 10)), "image/jpeg")),
    ]
    client.post("/owner/photos", data={CSRF_FIELD: _csrf(client)}, files=files)
    with Session(engine) as session:
        first, second = session.exec(select(Photo).order_by(Photo.id)).all()  # type: ignore[arg-type]
    assert first.is_main and not second.is_main
    client.post(f"/owner/photos/{second.id}", data={CSRF_FIELD: _csrf(client), "action": "main"})
    image = client.get(f"/owner/photos/{second.id}")
    assert image.status_code == 200 and image.headers["cache-control"] == "private, no-store"
    client.post(f"/owner/photos/{second.id}", data={CSRF_FIELD: _csrf(client), "action": "delete"})
    with Session(engine) as session:
        remaining = session.exec(select(Photo)).all()
    assert [p.id for p in remaining] == [first.id] and remaining[0].is_main


def test_owner_cannot_see_other_owners_photos(
    client: TestClient, engine: Engine, signed_in: int
) -> None:
    with Session(engine) as session:
        other = Business(slug="other", display_name="Other")
        session.add(other)
        session.commit()
        photo = Photo(business_id=other.id or 0, filename="x.jpg")
        session.add(photo)
        session.commit()
        photo_id = photo.id
    assert client.get(f"/owner/photos/{photo_id}").status_code == 404


def test_wrong_pin_and_lockout_messages(client: TestClient, engine: Engine) -> None:
    with Session(engine) as session:
        business = Business(slug="shop2", display_name="Shop")
        access = issue_access(business)
        session.add(business)
        session.commit()
    path = f"/o/{access.link_token}"
    wrong = "0000" if access.pin != "0000" else "1111"
    codes = []
    for _ in range(5):
        token = re.search(rf'name="{CSRF_FIELD}" value="([^"]+)"', client.get(path).text).group(1)  # type: ignore[union-attr]
        codes.append(client.post(path, data={CSRF_FIELD: token, "pin": wrong}).status_code)
    assert codes == [401, 401, 401, 401, 429]


# --- leftover inputs --------------------------------------------------------------------------


def test_stale_inputs_deleted_after_a_day(engine: Engine, tmp_path: Path) -> None:
    old, kept = tmp_path / "old.ogg", tmp_path / "kept.ogg"
    old.write_bytes(b"x")
    kept.write_bytes(b"x")
    long_ago = utcnow() - timedelta(days=2)
    with Session(engine) as session:
        session.add(IntakeSession(source="voice", input_path=str(old), created_at=long_ago))
        session.add(
            IntakeSession(
                source="voice", input_path=str(kept), keep_input=True, created_at=long_ago
            )
        )
        session.commit()
    assert purge_stale_inputs(engine, utcnow()) == 1
    assert not old.exists() and kept.exists()


def test_eval_cases_are_valid_json() -> None:
    for path in CASES.glob("*.json"):
        case = json.loads(path.read_text("utf-8"))
        assert {"id", "reviewed", "input", "expected"} <= set(case)
