import json
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sarvamai.errors import (
    ContentTooLargeError,
    PaymentRequiredError,
    ServiceUnavailableError,
    TooManyRequestsError,
    UnauthorizedError,
)
from sqlalchemy.engine import Engine
from sqlmodel import Session, select

from app.config import get_settings
from app.models import SttUsage
from app.services.security import CSRF_FIELD
from app.services.stt import (
    COST_INR_PER_MINUTE,
    MODE,
    MODEL,
    DisabledSTT,
    MockSTT,
    SarvamSTT,
    SttError,
    Transcript,
    clean_keyterms,
    get_stt,
    language_code,
    transcribe_note,
)
from app.services.uploads import UploadRejected

OGG = b"OggS" + b"\0" * 200
RESULT = {
    "transcript": "Chocolate cake 450 रुपये",
    "language_code": "mr-IN",
    "language_probability": 0.93,
    "timestamps": {
        "words": ["a", "b"],
        "start_time_seconds": [0.0, 10.0],
        "end_time_seconds": [9.5, 24.0],
    },
}


class FakeJob:
    def __init__(self, owner: "FakeClient", state: str = "Completed") -> None:
        self.owner, self.state = owner, state

    def upload_files(self, paths: list[str]) -> bool:
        path = Path(paths[0])
        self.owner.uploaded.append((path.name, path.read_bytes()))
        self.owner.temp_paths.append(path)
        return True

    def start(self) -> None:
        self.owner.calls.append("start")

    def wait_until_complete(self, poll_interval: int, timeout: int) -> Any:
        self.owner.calls.append(f"wait:{poll_interval}:{timeout}")
        return type("Status", (), {"job_state": self.state})()

    def download_outputs(self, folder: str) -> bool:
        result = {**RESULT, "timestamps": {"end_time_seconds": [30.0, 95.5]}}
        (Path(folder) / "note.json").write_text(json.dumps(result), "utf-8")
        return True


class FakeClient:
    """Stands in for sarvamai.SarvamAI: no network."""

    def __init__(
        self, rest_errors: list[Exception] | None = None, job_state: str = "Completed"
    ) -> None:
        self.rest_errors = rest_errors or []
        self.job_state = job_state
        self.rest_calls: list[dict[str, Any]] = []
        self.job_params: list[dict[str, Any]] = []
        self.uploaded: list[tuple[str, bytes]] = []
        self.temp_paths: list[Path] = []
        self.calls: list[str] = []
        self.speech_to_text = self
        self.speech_to_text_job = type("Jobs", (), {"create_job": self._create_job})()

    def transcribe(self, **kwargs: Any) -> dict[str, Any]:
        self.rest_calls.append(kwargs)
        if self.rest_errors:
            raise self.rest_errors.pop(0)
        return RESULT

    def _create_job(self, **kwargs: Any) -> FakeJob:
        self.job_params.append(kwargs)
        return FakeJob(self, self.job_state)


def _stt(client: FakeClient) -> SarvamSTT:
    return SarvamSTT(client, sleep=lambda _s: None)


# --- configuration ------------------------------------------------------------------------


def test_provider_from_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    assert isinstance(get_stt(get_settings()), DisabledSTT)
    monkeypatch.setenv("STT_PROVIDER", "mock")
    get_settings.cache_clear()
    assert isinstance(get_stt(get_settings()), MockSTT)


def test_sarvam_provider_uses_sdk_with_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("STT_PROVIDER", "sarvam")
    monkeypatch.setenv("STT_API_KEY", "test-key-not-real")
    get_settings.cache_clear()
    provider = get_stt(get_settings())
    assert isinstance(provider, SarvamSTT)
    assert type(provider.client).__name__ == "SarvamAI"  # built, but never called in tests


def test_sarvam_without_key_offers_typing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("STT_PROVIDER", "sarvam")
    get_settings.cache_clear()
    with pytest.raises(SttError) as caught:
        get_stt(get_settings())
    assert caught.value.key == "stt.unavailable"


def test_disabled_offers_typing() -> None:
    with pytest.raises(SttError) as caught:
        DisabledSTT().transcribe(OGG, "audio/ogg")
    assert caught.value.key == "stt.unavailable"


@pytest.mark.parametrize(
    ("lang", "code"),
    [("mr", "mr-IN"), ("hi", "hi-IN"), ("en", "en-IN"), (None, "unknown"), ("fr", "unknown")],
)
def test_language_codes(lang: str | None, code: str) -> None:
    assert language_code(lang) == code


def test_keyterms_cleaned_to_sarvam_limits() -> None:
    terms = ["  Ukadiche   modak ", "Ukadiche modak", "", "x" * 65, *[f"t{i}" for i in range(60)]]
    cleaned = clean_keyterms(terms)
    assert cleaned[0] == "Ukadiche modak"
    assert len(cleaned) == 50 and all(len(t) <= 64 for t in cleaned)


# --- Sarvam provider (fake SDK client) -------------------------------------------------------


def test_rest_request_uses_v4_codemix_and_keyterms() -> None:
    client = FakeClient()
    transcript = _stt(client).transcribe(OGG, "audio/ogg", "mr", ["Ukadiche modak"])
    call = client.rest_calls[0]
    assert (call["model"], call["mode"], call["language_code"]) == (MODEL, MODE, "mr-IN")
    assert (MODEL, MODE) == ("saaras:v4", "codemix")
    assert call["keyterms"] == ["Ukadiche modak"]
    assert call["with_timestamps"] is True
    assert call["file"] == ("note.ogg", OGG, "audio/ogg")
    assert call["request_options"] == {"timeout_in_seconds": 30, "max_retries": 0}
    assert transcript.text == "Chocolate cake 450 रुपये"
    assert (transcript.route, transcript.seconds, transcript.language) == ("rest", 24.0, "mr-IN")


def test_no_keyterms_field_when_none_given() -> None:
    client = FakeClient()
    _stt(client).transcribe(OGG, "audio/ogg")
    assert "keyterms" not in client.rest_calls[0]
    assert client.rest_calls[0]["language_code"] == "unknown"


def test_long_note_falls_back_to_batch_and_cleans_up() -> None:
    client = FakeClient(rest_errors=[ContentTooLargeError(body="audio too long")])
    transcript = _stt(client).transcribe(OGG, "audio/ogg", "hi", ["Nankhatai"])
    assert transcript.route == "batch" and transcript.seconds == 95.5
    params = client.job_params[0]
    assert (params["model"], params["mode"], params["language_code"]) == (MODEL, MODE, "hi-IN")
    assert params["keyterms"] == ["Nankhatai"] and params["with_timestamps"] is True
    assert client.uploaded == [("note.ogg", OGG)]
    assert client.calls == ["start", "wait:3:150"]
    assert not any(p.exists() for p in client.temp_paths)  # audio deleted after upload


def test_failed_batch_job_is_friendly() -> None:
    client = FakeClient(rest_errors=[ContentTooLargeError(body="long")], job_state="Failed")
    with pytest.raises(SttError) as caught:
        _stt(client).transcribe(OGG, "audio/ogg")
    assert caught.value.key == "stt.failed"
    assert not any(p.exists() for p in client.temp_paths)


def test_one_retry_on_rate_limit_then_success() -> None:
    client = FakeClient(rest_errors=[TooManyRequestsError(body="slow down")])
    assert _stt(client).transcribe(OGG, "audio/ogg").text
    assert len(client.rest_calls) == 2


def test_gives_up_after_one_retry() -> None:
    client = FakeClient(
        rest_errors=[
            ServiceUnavailableError(body="x"),
            ServiceUnavailableError(body="x"),
            ServiceUnavailableError(body="x"),
        ]
    )
    with pytest.raises(SttError) as caught:
        _stt(client).transcribe(OGG, "audio/ogg")
    assert caught.value.key == "stt.failed" and len(client.rest_calls) == 2


@pytest.mark.parametrize(
    "error", [UnauthorizedError(body="bad key"), PaymentRequiredError(body="no credits")]
)
def test_access_errors_not_retried_and_offer_typing(error: Exception) -> None:
    client = FakeClient(rest_errors=[error])
    with pytest.raises(SttError) as caught:
        _stt(client).transcribe(OGG, "audio/ogg")
    assert caught.value.key == "stt.unavailable" and len(client.rest_calls) == 1


def test_empty_transcript_is_friendly() -> None:
    client = FakeClient()
    client.transcribe = lambda **_k: {"transcript": "  "}  # type: ignore[method-assign]
    with pytest.raises(SttError) as caught:
        _stt(client).transcribe(OGG, "audio/ogg")
    assert caught.value.key == "stt.empty"


def test_friendly_messages_offer_typing_in_three_languages() -> None:
    from app.services.i18n import t

    for key in ("stt.unavailable", "stt.failed", "stt.empty", "stt.no_mock"):
        assert "type" in t(key, "en")
        assert "टाइप" in t(key, "hi") and "टाइप" in t(key, "mr")


# --- mock provider ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("sample", "lang", "word"),
    [
        ("sample-en", "en-IN", "Kothrud"),
        ("sample-hi", "hi-IN", "रुपये"),
        ("sample-mr", "mr-IN", "रुपये"),
    ],
)
def test_mock_samples(sample: str, lang: str, word: str) -> None:
    transcript = MockSTT().sample(sample)
    assert transcript.language == lang and word in transcript.text
    assert "450" in transcript.text and transcript.provider == "mock"
    assert 30 < transcript.seconds <= 120


def test_mock_unknown_audio_is_clear() -> None:
    with pytest.raises(SttError) as caught:
        MockSTT().transcribe(OGG, "audio/ogg")
    assert caught.value.key == "stt.no_mock"


def test_mock_sample_names_cannot_escape_fixture_folder() -> None:
    with pytest.raises(SttError):
        MockSTT().sample("../claude/example")


def test_mock_never_builds_sdk_client(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(*_a: object, **_k: object) -> None:
        raise AssertionError("Sarvam client created in mock mode")

    monkeypatch.setattr("app.services.stt.sarvam_client", refuse)
    monkeypatch.setenv("STT_PROVIDER", "mock")
    get_settings.cache_clear()
    assert isinstance(get_stt(get_settings()), MockSTT)


# --- transcribe_note: upload checks, usage and cost, logging -----------------------------------


def test_cost_estimate_per_minute() -> None:
    transcript = Transcript("x", "hi-IN", None, "sarvam", 90.0, "batch")
    assert transcript.cost_inr_estimate == pytest.approx(1.5 * COST_INR_PER_MINUTE)
    assert COST_INR_PER_MINUTE == 0.49


def test_usage_logged_per_business(engine: Engine, capsys: pytest.CaptureFixture[str]) -> None:
    from app.services.logs import setup_logging

    setup_logging(get_settings().secret_values())
    transcript = transcribe_note(
        engine, get_settings(), business_id=None, audio=OGG, language="mr", stt=_stt(FakeClient())
    )
    with Session(engine) as session:
        usage = session.exec(select(SttUsage)).one()
    assert (usage.seconds, usage.route, usage.model) == (24.0, "rest", "saaras:v4")
    assert usage.cost_inr_estimate == pytest.approx(24 / 60 * 0.49, abs=0.0001)
    logged = "".join(capsys.readouterr())
    assert "stt_done" in logged and "cost_inr_estimate" in logged
    assert transcript.text not in logged and "OggS" not in logged  # never the words or audio


def test_bad_uploads_rejected_before_any_stt_call(engine: Engine) -> None:
    client = FakeClient()
    for bad in (b"", b"#!/bin/sh\nrm -rf /", b"OggS" + b"\0" * (16 * 1024 * 1024)):
        with pytest.raises(UploadRejected):
            transcribe_note(engine, get_settings(), business_id=None, audio=bad, stt=_stt(client))
    assert client.rest_calls == []


# --- dev page -----------------------------------------------------------------------------------


def _token(client: TestClient) -> str:
    return client.get("/dev/stt").text.split(f'name="{CSRF_FIELD}" value="')[1].split('"')[0]


def test_dev_page_samples_in_mock_mode(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, engine: Engine
) -> None:
    monkeypatch.setenv("STT_PROVIDER", "mock")
    get_settings.cache_clear()
    response = client.post("/dev/stt", data={"sample": "sample-mr", CSRF_FIELD: _token(client)})
    assert response.status_code == 200
    assert "मी आशा" in response.text and "estimated cost ₹" in response.text
    with Session(engine) as session:
        assert session.exec(select(SttUsage)).one().is_mock


def test_dev_page_upload_unknown_audio_in_mock_mode(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("STT_PROVIDER", "mock")
    get_settings.cache_clear()
    response = client.post(
        "/dev/stt",
        data={CSRF_FIELD: _token(client), "language": "hi"},
        files={"audio": ("note.ogg", OGG, "audio/ogg")},
    )
    assert "no saved transcript" in response.text


def test_dev_page_offers_typing_when_off(client: TestClient) -> None:
    response = client.post(
        "/dev/stt",
        data={CSRF_FIELD: _token(client)},
        files={"audio": ("note.ogg", OGG, "audio/ogg")},
    )
    assert "Please type instead." in response.text
    assert "sample-en" not in response.text  # samples only in mock mode


def test_dev_page_hidden_in_production(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APP_ENV", "production")
    get_settings.cache_clear()
    from app.main import create_app

    with TestClient(create_app(), base_url="https://example.test") as prod:
        assert prod.get("/dev/stt").status_code == 404
