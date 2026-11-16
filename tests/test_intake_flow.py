import asyncio
import re
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.engine import Engine
from sqlmodel import Session, select

from app.config import get_settings
from app.models import Business, ClaudeUsage, IntakeSession, Photo, ProfileVersion
from app.services.intake import INTAKE_JOB, intake_runner
from app.services.jobs import JobWorker
from app.services.security import CSRF_FIELD
from app.services.stt import Transcript

OGG = b"OggS" + b"\0" * 300
TEXT = (
    "Priya Cakes in Baner, Pune. Chocolate cake 1250 for 1 kg, eggless available. "
    "Cupcakes 300 for a box of 6. Order 3 days ahead. WhatsApp 98765 43210."
)


def _jpeg() -> bytes:
    import io

    from PIL import Image

    out = io.BytesIO()
    Image.new("RGB", (64, 48), (200, 120, 60)).save(out, format="JPEG")
    return out.getvalue()


def _token(client: TestClient, path: str = "/start") -> str:
    match = re.search(rf'name="{CSRF_FIELD}" value="([^"]+)"', client.get(path).text)
    assert match
    return match.group(1)


def _run_jobs(engine: Engine) -> None:
    asyncio.run(
        JobWorker(engine, {INTAKE_JOB: intake_runner(engine, get_settings())}).run_until_idle()
    )


def _start(client: TestClient, engine: Engine, **data: Any) -> str:
    files = data.pop("files", None)
    form = {CSRF_FIELD: _token(client), "consent": "yes", "language": "en", **data}
    response = client.post("/start", data=form, files=files, follow_redirects=False)
    assert response.status_code == 303, response.text[:300]
    _run_jobs(engine)
    return response.headers["location"].split("/")[2]


def _cards(client: TestClient, token: str) -> tuple[str, dict[str, str]]:
    page = client.get(f"/start/{token}/confirm").text
    # Text and hidden inputs only: checkboxes such as "Remove this" are not ticked.
    fields = {
        k: v
        for k, v in re.findall(r'name="([^"]+)" value="([^"]*)"', page)
        if not k.endswith("_remove") and k not in ("languages", "order_methods", "action")
    }
    for name in ("business_type",):
        fields[name] = re.search(
            r'name="business_type">.*?value="(\w+)" selected', page, re.S
        ).group(1)  # type: ignore[union-attr]
    return page, fields


def _confirm(client: TestClient, token: str, fields: dict[str, Any], files: Any = None) -> Any:
    data = {**fields, "action": "confirm", "languages": ["en"], "order_methods": ["whatsapp"]}
    return client.post(f"/start/{token}/confirm", data=data, files=files)


def test_start_page_in_three_languages(client: TestClient) -> None:
    for lang, text in (
        ("en", "Record a voice note"),
        ("hi", "वॉइस नोट रिकॉर्ड करें"),
        ("mr", "व्हॉइस नोट रेकॉर्ड करा"),
    ):
        page = client.get(f"/start?lang={lang}").text
        assert text in page and 'src="/static/record.js"' in page
        assert 'name="consent"' in page and 'name="keep_input"' in page


def test_consent_is_required(client: TestClient) -> None:
    response = client.post(
        "/start", data={CSRF_FIELD: _token(client), "source": "text", "text": TEXT}
    )
    assert response.status_code == 422 and "tick the box" in response.text


def test_text_too_short(client: TestClient) -> None:
    response = client.post(
        "/start",
        data={CSRF_FIELD: _token(client), "source": "text", "consent": "yes", "text": "cake"},
    )
    assert response.status_code == 422


def test_typed_intake_to_business_with_owner_access(client: TestClient, engine: Engine) -> None:
    token = _start(client, engine, source="text", text=TEXT)
    wait = client.get(f"/start/{token}/wait", follow_redirects=False)
    assert wait.headers["location"] == f"/start/{token}/confirm"
    page, fields = _cards(client, token)
    assert fields["web_address"] == "priya-cakes" and fields["business_name"] == "Priya Cakes"
    assert "Read from your photo" not in page
    fields["p0_o0_price"] = "1,300"  # she corrects a price, with a comma
    done = _confirm(client, token, fields)
    assert done.status_code == 200
    assert done.headers["cache-control"] == "no-store"
    link = re.search(r'href="(http://localhost:8000/o/[^"]+)"', done.text).group(1)  # type: ignore[union-attr]
    pin = re.search(r'class="secret-value pin">(\d{4})<', done.text).group(1)  # type: ignore[union-attr]
    with Session(engine) as session:
        business = session.exec(select(Business)).one()
        version = session.exec(select(ProfileVersion)).one()
        intake = session.exec(select(IntakeSession)).one()
        usage = session.exec(select(ClaudeUsage)).all()
    assert business.slug == "priya-cakes" and not business.is_sample
    assert business.consent_given_at is not None and "I agree" in str(business.consent_text)
    assert pin not in str(business.pin_hash) and business.owner_link_hash not in link
    assert version.version == 1 and version.profile["products"][0]["options"][0]["price"] == 1300
    assert {
        "field": "products[chocolate cake].options[1 kg].price",
        "before": 1250,
        "after": 1300,
    } in (intake.corrections or [])
    assert all(u.business_id == business.id for u in usage)
    # PIN entry signs her in
    path = link.replace("http://localhost:8000", "")
    pin_page = client.get(path)
    assert "Enter your PIN" in pin_page.text
    signed_in = client.post(
        path, data={CSRF_FIELD: _token(client, path), "pin": pin}, follow_redirects=False
    )
    assert signed_in.headers["location"] == "/owner"
    assert "Priya Cakes" in client.get("/owner").text


def test_sample_voice_note_creates_a_sample_business(client: TestClient, engine: Engine) -> None:
    response = client.post(
        "/start", data={CSRF_FIELD: _token(client), "sample": "sample-mr"}, follow_redirects=False
    )
    _run_jobs(engine)
    token = response.headers["location"].split("/")[2]
    page, fields = _cards(client, token)
    assert "तुमचा WhatsApp नंबर काय आहे?" in page
    fields["phone"] = "९८२२० १२३४५"
    assert _confirm(client, token, fields).status_code == 200
    with Session(engine) as session:
        business = session.exec(select(Business)).one()
    assert business.is_sample


def test_voice_note_deleted_after_processing(
    client: TestClient, engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    class FakeSTT:
        name = "fake"

        def transcribe(
            self, audio: bytes, mime: str, language: str | None = None, keyterms: Any = ()
        ) -> Transcript:
            return Transcript(TEXT, "en-IN", 0.9, "sarvam", 45.0, "batch")

    monkeypatch.setattr("app.services.intake.get_stt", lambda _s: FakeSTT())
    token = _start(client, engine, source="voice", files={"upload": ("note.ogg", OGG, "audio/ogg")})
    with Session(engine) as session:
        intake = session.exec(select(IntakeSession)).one()
    assert intake.status == "ready" and intake.transcript == TEXT and intake.stt_route == "batch"
    assert intake.input_path is None
    assert not (get_settings().uploads_dir / "intake").exists() or not any(
        (get_settings().uploads_dir / "intake").rglob("input*")
    )
    assert token


def test_voice_note_kept_when_she_agrees(
    client: TestClient, engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "app.services.intake.get_stt",
        lambda _s: type(
            "S",
            (),
            {
                "name": "fake",
                "transcribe": lambda *a, **k: Transcript(
                    TEXT, "en-IN", 0.9, "sarvam", 30.0, "rest"
                ),
            },
        )(),
    )
    _start(
        client,
        engine,
        source="voice",
        keep_input="yes",
        files={"upload": ("note.ogg", OGG, "audio/ogg")},
    )
    with Session(engine) as session:
        intake = session.exec(select(IntakeSession)).one()
    assert intake.input_path and Path(intake.input_path).read_bytes() == OGG


def test_speech_failure_offers_typing(client: TestClient, engine: Engine) -> None:
    token = _start(client, engine, source="voice", files={"upload": ("note.ogg", OGG, "audio/ogg")})
    page = client.get(f"/start/{token}/wait").text
    assert "Please type instead." in page  # STT_PROVIDER=none in tests


def test_photo_prices_are_marked_for_checking(client: TestClient, engine: Engine) -> None:
    token = _start(
        client, engine, source="photo", files={"upload": ("menu.jpg", _jpeg(), "image/jpeg")}
    )
    page, _ = _cards(client, token)
    assert page.count("Read from your photo") == 3


def test_card_actions_and_errors(client: TestClient, engine: Engine) -> None:
    token = _start(client, engine, source="text", text=TEXT)
    _, fields = _cards(client, token)
    added = client.post(f"/start/{token}/confirm", data={**fields, "action": "add_product"})
    assert 'name="p2_name"' in added.text
    bad = {**fields, "area": "Flat 12, Shanti Society", "web_address": "a", "p0_o0_price": "abc"}
    response = _confirm(client, token, bad)
    assert response.status_code == 422
    for text in ("not your full address", "3 to 40 letters", "as a number"):
        assert text in response.text


def test_taken_web_address(client: TestClient, engine: Engine) -> None:
    with Session(engine) as session:
        session.add(Business(slug="priya-cakes", display_name="Other"))
        session.commit()
    token = _start(client, engine, source="text", text=TEXT)
    _, fields = _cards(client, token)
    assert fields["web_address"] == "priya-cakes-2"
    fields["web_address"] = "priya-cakes"
    assert "already taken" in _confirm(client, token, fields).text


def test_photos_at_confirm_are_stored_re_encoded(client: TestClient, engine: Engine) -> None:
    token = _start(client, engine, source="text", text=TEXT)
    _, fields = _cards(client, token)
    files = [
        ("photos", ("a.jpg", _jpeg(), "image/jpeg")),
        ("photos", ("b.jpg", _jpeg(), "image/jpeg")),
    ]
    assert _confirm(client, token, fields, files).status_code == 200
    with Session(engine) as session:
        photos = session.exec(select(Photo)).all()
    assert [p.is_main for p in photos] == [True, False]
    bad = _start(client, engine, source="text", text=TEXT)
    _, fields = _cards(client, bad)
    fields["web_address"] = "another-shop"
    response = _confirm(client, bad, fields, [("photos", ("x.jpg", b"not an image", "image/jpeg"))])
    assert response.status_code == 422


def test_owner_pages_need_sign_in(client: TestClient) -> None:
    assert client.get("/owner").status_code == 401
    assert client.get("/owner/photos/1").status_code == 401
    assert client.get("/o/not-a-real-link").status_code == 404


def test_transcripts_never_logged(
    client: TestClient, engine: Engine, capsys: pytest.CaptureFixture[str]
) -> None:
    from app.services.logs import setup_logging

    setup_logging(get_settings().secret_values())
    _start(client, engine, source="text", text=TEXT)
    out = "".join(capsys.readouterr())
    assert "intake_started" in out and "extract_done" in out
    assert "Chocolate cake 1250" not in out and "eggless available" not in out
