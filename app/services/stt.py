"""Speech-to-text behind an interface; the provider is chosen by STT_PROVIDER.

- sarvam: Sarvam AI (official sarvamai SDK), Saaras v4 in codemix mode (native script, English
  words kept in English, numbers as digits), with optional key terms such as product names.
  Short notes use the REST endpoint; when it turns a recording away as too long, the batch API
  takes it (voice notes of 1 to 2 minutes).
- mock: saved transcripts from tests/fixtures/stt/ (tests and demos, no credits needed).
- none: speech-to-text is off; the owner is offered typing.

Audio is never logged. It is written to disk only for the batch upload, in a private temporary
folder that is deleted straight after. Every failure becomes SttError with a friendly key that
offers typing instead.
"""

import hashlib
import json
import logging
import tempfile
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol
from zoneinfo import ZoneInfo

from sqlalchemy.engine import Engine
from sqlmodel import Session

from app.config import Settings
from app.models import SttUsage
from app.services.errors import AppError
from app.services.uploads import check_audio

logger = logging.getLogger(__name__)

MODEL = "saaras:v4"
MODE = "codemix"
REST_TIMEOUT_SECONDS = 30.0
BATCH_WAIT_SECONDS = 150
BATCH_POLL_SECONDS = 3
RETRY_DELAY_SECONDS = 2.0
MAX_KEYTERMS = 50
MAX_KEYTERM_CHARS = 64
MAX_NOTE_SECONDS = 120
COST_INR_PER_MINUTE = 0.49  # Sarvam's published pay-as-you-go rate; an estimate only
LANGUAGE_CODES = {"en": "en-IN", "hi": "hi-IN", "mr": "mr-IN"}
EXTENSIONS = {
    "audio/ogg": ".ogg",
    "audio/webm": ".webm",
    "audio/wav": ".wav",
    "audio/mpeg": ".mp3",
    "audio/mp4": ".m4a",
}
STT_SAMPLES = ("sample-en", "sample-hi", "sample-mr")
FIXTURE_DIR = Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "stt"


class SttError(AppError):
    """Speech-to-text failed; key is a friendly message that offers typing instead."""

    def __init__(self, key: str = "stt.failed") -> None:
        super().__init__(key, status_code=503)


@dataclass(frozen=True)
class Transcript:
    """The text of a voice note and what it cost."""

    text: str
    language: str | None
    confidence: float | None
    provider: str
    seconds: float
    route: str  # rest, batch or mock

    @property
    def cost_inr_estimate(self) -> float:
        """Estimated cost at the published per-minute rate."""
        return round(self.seconds / 60 * COST_INR_PER_MINUTE, 4)


class SpeechToText(Protocol):
    """A speech-to-text provider."""

    name: str

    def transcribe(
        self,
        audio: bytes,
        mime: str,
        language: str | None = None,
        keyterms: Sequence[str] = (),
    ) -> Transcript:
        """Return the transcript of one voice note, or raise SttError."""
        ...


def clean_keyterms(keyterms: Sequence[str]) -> list[str]:
    """At most 50 distinct terms of at most 64 characters (Sarvam's limits)."""
    cleaned: list[str] = []
    for term in keyterms:
        term = " ".join(term.split())
        if term and len(term) <= MAX_KEYTERM_CHARS and term not in cleaned:
            cleaned.append(term)
    return cleaned[:MAX_KEYTERMS]


def language_code(language: str | None) -> str:
    """Owner language (en, hi, mr) to a Sarvam code; unknown lets Sarvam detect it."""
    return LANGUAGE_CODES.get(language or "", "unknown")


def speech_seconds(result: Any) -> float:
    """Seconds of speech, from the end of the last timestamped chunk (0 if not given)."""
    timestamps = _get(result, "timestamps")
    ends = _get(timestamps, "end_time_seconds") if timestamps is not None else None
    return float(max(ends)) if ends else 0.0


def _get(obj: Any, name: str) -> Any:
    return obj.get(name) if isinstance(obj, dict) else getattr(obj, name, None)


class DisabledSTT:
    """STT_PROVIDER=none: voice is off and the owner types instead."""

    name = "none"

    def transcribe(
        self,
        audio: bytes,
        mime: str,
        language: str | None = None,
        keyterms: Sequence[str] = (),
    ) -> Transcript:
        """Always ask the owner to type."""
        raise SttError("stt.unavailable")


class MockSTT:
    """Saved transcripts for tests and demos. Unknown audio gets a clear 'no saved transcript'."""

    name = "mock"

    def __init__(self, fixture_dir: Path = FIXTURE_DIR) -> None:
        self.fixture_dir = fixture_dir

    @staticmethod
    def audio_key(audio: bytes) -> str:
        """Fixture name for a recording: the first 16 hex characters of its SHA-256."""
        return hashlib.sha256(audio).hexdigest()[:16]

    def sample(self, sample_id: str) -> Transcript:
        """A built-in sample voice note (sample-en, sample-hi, sample-mr)."""
        return self._load(sample_id)

    def transcribe(
        self,
        audio: bytes,
        mime: str,
        language: str | None = None,
        keyterms: Sequence[str] = (),
    ) -> Transcript:
        """Return the saved transcript for this exact recording."""
        return self._load(self.audio_key(audio))

    def _load(self, key: str) -> Transcript:
        path = (self.fixture_dir / f"{key}.json").resolve()
        if not path.is_relative_to(self.fixture_dir.resolve()) or not path.is_file():
            raise SttError("stt.no_mock")
        data = json.loads(path.read_text(encoding="utf-8"))
        return Transcript(
            text=str(data["transcript"]),
            language=data.get("language_code"),
            confidence=data.get("language_probability"),
            provider="mock",
            seconds=float(data.get("seconds", 0)),
            route="mock",
        )


class SarvamSTT:
    """Sarvam AI through its official SDK: REST for short notes, batch for long ones."""

    name = "sarvam"

    def __init__(self, client: Any, sleep: Callable[[float], None] = time.sleep) -> None:
        self.client = client
        self.sleep = sleep

    def transcribe(
        self,
        audio: bytes,
        mime: str,
        language: str | None = None,
        keyterms: Sequence[str] = (),
    ) -> Transcript:
        """Transcribe one voice note (REST first, batch when it is too long for REST)."""
        from sarvamai.errors import (
            BadRequestError,
            ContentTooLargeError,
            UnprocessableEntityError,
        )

        params = self._params(language, keyterms)
        try:
            result = self._with_retry(lambda: self._rest(audio, mime, params))
            return self._transcript(result, "rest")
        except (ContentTooLargeError, BadRequestError, UnprocessableEntityError):
            logger.info("stt_batch_fallback", extra={"bytes": len(audio)})
        return self._transcript(self._batch(audio, mime, params), "batch")

    @staticmethod
    def _params(language: str | None, keyterms: Sequence[str]) -> dict[str, Any]:
        params: dict[str, Any] = {
            "model": MODEL,
            "mode": MODE,
            "language_code": language_code(language),
        }
        terms = clean_keyterms(keyterms)
        if terms:
            params["keyterms"] = terms
        return params

    def _rest(self, audio: bytes, mime: str, params: dict[str, Any]) -> Any:
        name = f"note{EXTENSIONS.get(mime, '.wav')}"
        return self.client.speech_to_text.transcribe(
            file=(name, audio, mime),
            with_timestamps=True,
            # The SDK's own retries are off: we retry once ourselves.
            request_options={"timeout_in_seconds": int(REST_TIMEOUT_SECONDS), "max_retries": 0},
            **params,
        )

    def _batch(self, audio: bytes, mime: str, params: dict[str, Any]) -> Any:
        """Upload to the batch API, wait, and read the single result; temp files are removed."""
        with tempfile.TemporaryDirectory(prefix="stt-") as folder:
            path = Path(folder) / f"note{EXTENSIONS.get(mime, '.wav')}"
            path.write_bytes(audio)
            try:
                job = self.client.speech_to_text_job.create_job(with_timestamps=True, **params)
                job.upload_files([str(path)])
                job.start()
                status = job.wait_until_complete(
                    poll_interval=BATCH_POLL_SECONDS, timeout=BATCH_WAIT_SECONDS
                )
                if str(getattr(status, "job_state", "")).lower() != "completed":
                    raise SttError("stt.failed")
                out = Path(folder) / "out"
                out.mkdir()
                job.download_outputs(str(out))
                results = list(out.glob("*.json"))
                if not results:
                    raise SttError("stt.failed")
                return json.loads(results[0].read_text(encoding="utf-8"))
            except SttError:
                raise
            except Exception as exc:
                raise self._friendly(exc) from exc

    def _with_retry(self, call: Callable[[], Any]) -> Any:
        """One retry on rate limits, server errors and timeouts; access errors are not retried."""
        from sarvamai.errors import (
            BadRequestError,
            ContentTooLargeError,
            UnprocessableEntityError,
        )

        for attempt in (1, 2):
            try:
                return call()
            except (ContentTooLargeError, BadRequestError, UnprocessableEntityError):
                raise
            except Exception as exc:
                friendly = self._friendly(exc)
                if friendly.key != "stt.failed" or attempt == 2 or not self._retryable(exc):
                    raise friendly from exc
                logger.warning("stt_retry", extra={"error": type(exc).__name__})
                self.sleep(RETRY_DELAY_SECONDS)
        raise SttError("stt.failed")  # pragma: no cover

    @staticmethod
    def _retryable(exc: Exception) -> bool:
        from sarvamai.errors import (
            BadGatewayError,
            GatewayTimeoutError,
            InternalServerError,
            ServiceUnavailableError,
            TooManyRequestsError,
        )

        retryable = (
            TooManyRequestsError,
            InternalServerError,
            BadGatewayError,
            ServiceUnavailableError,
            GatewayTimeoutError,
            TimeoutError,
            ConnectionError,
        )
        return isinstance(exc, retryable) or type(exc).__name__ in ("ReadTimeout", "ConnectTimeout")

    @staticmethod
    def _friendly(exc: Exception) -> SttError:
        from sarvamai.errors import ForbiddenError, PaymentRequiredError, UnauthorizedError

        if isinstance(exc, UnauthorizedError | ForbiddenError | PaymentRequiredError):
            return SttError("stt.unavailable")
        return SttError("stt.failed")

    @staticmethod
    def _transcript(result: Any, route: str) -> Transcript:
        text = str(_get(result, "transcript") or "").strip()
        if not text:
            raise SttError("stt.empty")
        probability = _get(result, "language_probability")
        return Transcript(
            text=text,
            language=_get(result, "language_code"),
            confidence=float(probability) if probability is not None else None,
            provider="sarvam",
            seconds=speech_seconds(result),
            route=route,
        )


def sarvam_client(settings: Settings) -> Any:
    """The official SDK client, authenticated with STT_API_KEY."""
    from sarvamai import SarvamAI

    if settings.stt_api_key is None:
        raise SttError("stt.unavailable")
    return SarvamAI(
        api_subscription_key=settings.stt_api_key.get_secret_value(), timeout=REST_TIMEOUT_SECONDS
    )


def get_stt(settings: Settings) -> SpeechToText:
    """The configured provider."""
    if settings.stt_provider == "mock":
        return MockSTT()
    if settings.stt_provider == "sarvam":
        return SarvamSTT(sarvam_client(settings))
    return DisabledSTT()


IST = ZoneInfo("Asia/Kolkata")


def record_usage(engine: Engine, business_id: int | None, transcript: Transcript) -> None:
    """Store the audio duration and estimated cost for this business."""
    with Session(engine) as session:
        session.add(
            SttUsage(
                business_id=business_id,
                provider=transcript.provider,
                model=MODEL,
                route=transcript.route,
                seconds=round(transcript.seconds, 1),
                cost_inr_estimate=transcript.cost_inr_estimate,
                is_mock=transcript.provider == "mock",
                day_ist=datetime.now(UTC).astimezone(IST).date(),
            )
        )
        session.commit()


def transcribe_note(
    engine: Engine,
    settings: Settings,
    *,
    business_id: int | None,
    audio: bytes,
    language: str | None = None,
    keyterms: Sequence[str] = (),
    stt: SpeechToText | None = None,
) -> Transcript:
    """Check the upload, transcribe it, and log duration and estimated cost (never the audio)."""
    checked = check_audio(audio)
    provider = stt or get_stt(settings)
    started = time.monotonic()
    try:
        transcript = provider.transcribe(checked.data, checked.mime, language, keyterms)
    except SttError as exc:
        logger.warning(
            "stt_failed",
            extra={
                "provider": provider.name,
                "key": exc.key,
                "bytes": len(audio),
                "business_id": business_id,
            },
        )
        raise
    record_usage(engine, business_id, transcript)
    if transcript.seconds > MAX_NOTE_SECONDS:
        logger.info("stt_long_note", extra={"seconds": transcript.seconds})
    logger.info(
        "stt_done",
        extra={
            "provider": transcript.provider,
            "route": transcript.route,
            "business_id": business_id,
            "language": transcript.language,
            "seconds": transcript.seconds,
            "bytes": len(audio),
            "cost_inr_estimate": transcript.cost_inr_estimate,
            "ms": round((time.monotonic() - started) * 1000),
        },
    )
    return transcript
