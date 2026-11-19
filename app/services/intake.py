"""Voice, photo and typed intake: from the owner's words to a confirmed business with owner access.

1. start_intake saves the input (audio or a re-encoded photo, or typed text) and queues a job.
2. run_intake (background job) transcribes voice notes, runs extraction, stores Claude's first
   guess and the checked draft, and deletes the input unless the owner agreed to keep it.
3. The confirmation cards edit the draft (draft_from_form).
4. confirm_intake creates the Business, profile version 1, the owner's consent record and photos,
   records every correction for accuracy measurement, and issues the private link and PIN.
"""

import asyncio
import logging
import secrets
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sqlalchemy import update
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, col, select

from app.config import Settings
from app.models import Business, ClaudeUsage, IntakeSession, Job, ProfileVersion, SttUsage, utcnow
from app.schemas import BusinessProfile, normalise_phone
from app.services import web_address
from app.services.accuracy import corrections
from app.services.address_guard import looks_like_address
from app.services.auth import OwnerAccess, hash_token, issue_access
from app.services.errors import AppError
from app.services.extract import FIELDS, ExtractResult, extract_profile
from app.services.field_rules import business_name_problem
from app.services.i18n import normalise_language, t
from app.services.jobs import Runner, enqueue
from app.services.numbers import ascii_digits
from app.services.photos import add_photo
from app.services.stt import MockSTT, SttError, get_stt, transcribe_note
from app.services.uploads import UploadRejected, check_audio, check_image

logger = logging.getLogger(__name__)

INTAKE_JOB = "intake"
INTAKE_TIMEOUT_SECONDS = 420  # batch speech-to-text (up to ~3 min) plus extraction
MIN_TEXT = 10
MAX_TEXT = 5000
MAX_PRODUCTS = 40
MAX_OPTIONS = 12
SOURCES = ("voice", "text", "photo")
EXTRACT_ERRORS = {
    "failed": "intake.extract_failed",
    "spend_cap": "error.spend_cap",
    "refused": "intake.extract_failed",
    "invalid": "intake.extract_failed",
}


@dataclass
class Started:
    """A new intake: its id and the token for its private URL (shown once)."""

    intake_id: int
    token: str


@dataclass
class Confirmed:
    """A confirmed business and the owner access to show once."""

    business: Business
    access: OwnerAccess


@dataclass
class CardErrors:
    """Field -> i18n key for the confirmation cards."""

    errors: dict[str, str] = field(default_factory=dict)

    def add(self, name: str, key: str) -> None:
        """Record one error (the first per field wins)."""
        self.errors.setdefault(name, key)


def intake_dir(settings: Settings, intake_id: int) -> Path:
    """Folder for one intake's input while it waits to be processed."""
    return settings.uploads_dir / "intake" / str(intake_id)


def start_intake(
    engine: Engine,
    settings: Settings,
    *,
    source: str,
    language: str | None,
    consent: bool,
    keep_input: bool,
    text: str = "",
    data: bytes = b"",
) -> Started:
    """Check and save the input, create the intake and queue its job."""
    if source not in SOURCES:
        raise AppError("error.invalid_input", status_code=422)
    if not consent:
        raise AppError("intake.consent_required", status_code=422)
    lang = normalise_language(language) or "en"
    saved: tuple[bytes, str] | None = None
    if source == "voice":
        audio = check_audio(data)
        saved = (
            audio.data,
            {
                "audio/ogg": ".ogg",
                "audio/webm": ".webm",
                "audio/wav": ".wav",
                "audio/mpeg": ".mp3",
                "audio/mp4": ".m4a",
            }[audio.mime],
        )
    elif source == "photo":
        image = check_image(data)
        saved = (
            image.data,
            {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp"}[image.mime],
        )
    else:
        text = text.strip()
        if not MIN_TEXT <= len(text) <= MAX_TEXT:
            raise AppError("intake.text_length", status_code=422)
    token = secrets.token_urlsafe(24)
    with Session(engine) as session:
        intake = IntakeSession(
            source=source,
            language=lang,
            token_hash=hash_token(token),
            keep_input=keep_input,
            consent_text=t("intake.consent", lang),
            transcript=text if source == "text" else None,
        )
        session.add(intake)
        session.commit()
        session.refresh(intake)
        assert intake.id is not None  # noqa: S101
        if saved:
            folder = intake_dir(settings, intake.id)
            folder.mkdir(parents=True, exist_ok=True)
            path = folder / f"input{saved[1]}"
            path.write_bytes(saved[0])
            intake.input_path = str(path)
        intake.job_id = enqueue(
            engine,
            INTAKE_JOB,
            payload={"intake_id": intake.id},
            timeout_seconds=INTAKE_TIMEOUT_SECONDS,
            max_attempts=1,
        )
        session.add(intake)
        session.commit()
        logger.info(
            "intake_started",
            extra={
                "intake_id": intake.id,
                "source": source,
                "lang": lang,
                "keep_input": keep_input,
            },
        )
        return Started(intake.id, token)


def is_sample_intake(intake: IntakeSession) -> bool:
    """Intakes from the built-in sample voice notes are demos, never real businesses."""
    return bool(intake.stt_route and intake.stt_route.startswith("sample:"))


def start_sample(
    engine: Engine, settings: Settings, sample_id: str, language: str | None
) -> Started:
    """Start an intake from a built-in sample voice note (mock speech-to-text)."""
    transcript = MockSTT().sample(sample_id)
    started = start_intake(
        engine,
        settings,
        source="text",
        language=language,
        consent=True,
        keep_input=False,
        text=transcript.text,
    )
    with Session(engine) as session:
        intake = session.get(IntakeSession, started.intake_id)
        assert intake is not None  # noqa: S101
        intake.source = "voice"
        intake.stt_route = f"sample:{sample_id}"
        session.add(intake)
        session.commit()
    return started


def intake_for_token(session: Session, token: str) -> IntakeSession:
    """The intake behind a private token, or a friendly not-found error."""
    if token and len(token) <= 64:
        found = session.exec(
            select(IntakeSession).where(IntakeSession.token_hash == hash_token(token))
        ).first()
        if found is not None:
            return found
    raise AppError("error.not_found", status_code=404)


def _fixture_key(intake: IntakeSession) -> str:
    if intake.stt_route and intake.stt_route.startswith("sample:"):
        return f"extract_{intake.stt_route.split(':', 1)[1]}"
    return "extract_photo" if intake.source == "photo" else "extract_default"


def _transcribe(engine: Engine, settings: Settings, intake: IntakeSession, data: bytes) -> str:
    result = transcribe_note(
        engine,
        settings,
        business_id=None,
        audio=data,
        language=intake.language,
        intake_id=intake.id,
        stt=get_stt(settings),
    )
    intake.stt_route = result.route
    return result.text


def run_intake(engine: Engine, settings: Settings, intake_id: int) -> None:
    """Background job: speech-to-text if needed, extraction, then delete the input."""
    with Session(engine) as session:
        intake = session.get(IntakeSession, intake_id)
        if intake is None or intake.status not in ("queued", "processing"):
            return
        intake.status = "processing"
        session.add(intake)
        session.commit()
        try:
            data = Path(intake.input_path).read_bytes() if intake.input_path else b""
            if intake.source == "voice" and data:
                intake.transcript = _transcribe(engine, settings, intake, data)
            result = extract_profile(
                engine,
                settings,
                source=intake.source,  # type: ignore[arg-type]
                language=intake.language,
                transcript=intake.transcript,
                image=data if intake.source == "photo" else None,
                image_mime=_mime(intake.input_path),
                intake_id=intake.id,
                fixture_key=_fixture_key(intake),
                saved_answer=_saved_sample_answer(intake),
            )
            _store_result(session, intake, result)
        except (SttError, UploadRejected) as exc:
            intake.status, intake.error_key = "failed", exc.key
        except Exception:
            logger.exception("intake_error", extra={"intake_id": intake_id})
            intake.status, intake.error_key = "failed", "intake.extract_failed"
        finally:
            _delete_input(intake)
            session.add(intake)
            session.commit()


SAVED_SAMPLE_DIR = Path(__file__).resolve().parents[1] / "samples" / "extract"


def _saved_sample_answer(intake: IntakeSession) -> Path | None:
    """For the built-in sample voice notes: the saved extraction used if the spend cap is hit."""
    if not is_sample_intake(intake) or intake.stt_route is None:
        return None
    sample_id = intake.stt_route.removeprefix("sample:")
    path = (SAVED_SAMPLE_DIR / f"{sample_id}.json").resolve()
    return path if path.parent == SAVED_SAMPLE_DIR.resolve() else None


def _mime(path: str | None) -> str:
    suffix = Path(path).suffix if path else ""
    return {".png": "image/png", ".webp": "image/webp"}.get(suffix, "image/jpeg")


def _store_result(session: Session, intake: IntakeSession, result: ExtractResult) -> None:
    if result.status != "done" or result.profile is None:
        intake.status, intake.error_key = "failed", EXTRACT_ERRORS[result.status]
        return
    intake.first_guess = result.first_guess
    intake.draft = result.profile.model_dump(mode="json")
    name = result.profile.business_name
    intake.web_address = web_address.suggest(session, name, result.web_address)
    intake.status = "ready"


def _delete_input(intake: IntakeSession) -> None:
    """Delete audio or photo after processing unless the owner agreed to keep it."""
    if intake.input_path and not intake.keep_input:
        path = Path(intake.input_path)
        path.unlink(missing_ok=True)
        if path.parent.exists() and not any(path.parent.iterdir()):
            path.parent.rmdir()
        intake.input_path = None


def intake_runner(engine: Engine, settings: Settings) -> Runner:
    """Job runner for intakes (runs in a thread: speech-to-text and Claude block)."""

    async def runner(job_id: str) -> None:
        with Session(engine) as session:
            job = session.get(Job, job_id)
            intake_id = int((job.payload or {}).get("intake_id", 0)) if job else 0
        await asyncio.to_thread(run_intake, engine, settings, intake_id)

    return runner


# --- confirmation cards ------------------------------------------------------------------


def _price(value: str) -> int | None:
    cleaned = ascii_digits(value).replace(",", "").replace("₹", "").replace(" ", "").strip()
    if not cleaned:
        return None
    if not cleaned.isdigit():
        raise ValueError(value)
    return int(cleaned)


def _count(value: Any) -> int:
    """A card count from the form; anything that is not a small whole number counts as 0."""
    text = str(value or "").strip()
    return int(text) if text.isdigit() and len(text) <= 3 else 0


def _list(value: str) -> list[str]:
    return [p.strip() for p in value.replace("\n", ",").split(",") if p.strip()]


def draft_from_form(
    form: Mapping[str, Any], multi: Mapping[str, Sequence[str]], errors: CardErrors
) -> dict[str, Any]:
    """Read the confirmation cards back into a profile dict (errors are collected)."""
    draft: dict[str, Any] = {
        "business_name": str(form.get("business_name", "")).strip(),
        "business_type": str(form.get("business_type", "other")),
        "area": str(form.get("area", "")).strip(),
        "delivery_areas": _list(str(form.get("delivery_areas", ""))),
        "languages": [v for v in multi.get("languages", []) if v in ("en", "hi", "mr")],
        # "I don't want an order button" (only without a phone): no order methods at all.
        "order_methods": []
        if form.get("no_order_button") == "yes" and not str(form.get("phone", "")).strip()
        else list(multi.get("order_methods", [])),
        "lead_time": str(form.get("lead_time", "")).strip(),
        "phone": ascii_digits(str(form.get("phone", ""))).strip(),
        "policy": str(form.get("policy", "")).strip(),
        "missing": list(multi.get("missing", [])),
        "products": [],
    }
    count = min(_count(form.get("product_count")), MAX_PRODUCTS)
    for i in range(count):
        if form.get(f"p{i}_remove"):
            continue
        product = _product_from_form(form, i, len(draft["products"]), errors)
        if product:
            draft["products"].append(product)
    return draft


def _product_from_form(
    form: Mapping[str, Any], i: int, out: int, errors: CardErrors
) -> dict[str, Any] | None:
    """Read product card i, which will be shown as card number `out` (errors use `out`)."""
    name = str(form.get(f"p{i}_name", "")).strip()
    options: list[dict[str, Any]] = []
    for j in range(min(_count(form.get(f"p{i}_option_count")), MAX_OPTIONS)):
        label = str(form.get(f"p{i}_o{j}_label", "")).strip()
        raw = str(form.get(f"p{i}_o{j}_price", "")).strip()
        if not label and not raw:
            continue  # an empty row
        key = f"p{out}_o{len(options)}_price"
        try:
            price = _price(raw)
        except ValueError:
            errors.add(key, "intake.price_invalid")
            options.append({"label": label, "price": raw})  # keep what the owner typed
            continue
        if price is None:
            errors.add(key, "intake.price_needed")
            options.append({"label": label, "price": ""})
            continue
        options.append({"label": label, "price": price})
    if not name and not options:
        return None
    if not name:
        errors.add(f"p{out}_name", "intake.name_needed")
    return {
        "name": name,
        "unit": str(form.get(f"p{i}_unit", "service")),
        "options": options,
        "notes": str(form.get(f"p{i}_notes", "")).strip(),
    }


def validate_draft(
    session: Session, draft: dict[str, Any], address: str, errors: CardErrors
) -> BusinessProfile | None:
    """Final checks before confirming: required name, phone, no addresses, free web address."""
    if not draft["business_name"]:
        errors.add("business_name", "intake.name_needed")
    elif problem := business_name_problem(draft["business_name"]):
        errors.add("business_name", problem)
    if draft["phone"]:
        try:
            draft["phone"] = normalise_phone(draft["phone"])
        except ValueError:
            errors.add("phone", "intake.phone_invalid")
    if draft["area"] and looks_like_address(draft["area"]):
        errors.add("area", "intake.address_not_allowed")
    if any(looks_like_address(a) for a in draft["delivery_areas"]):
        errors.add("delivery_areas", "intake.address_not_allowed")
    problem = web_address.problem(address)
    if problem:
        errors.add("web_address", problem)
    elif web_address.is_taken(session, address):
        errors.add("web_address", "intake.address_taken")
    if errors.errors:
        return None
    filled = {f for f in FIELDS if draft.get(f)}
    draft["missing"] = [
        m for m in draft["missing"] if m.split(":", 1)[0].strip() not in filled | {"products"}
    ]
    try:
        return BusinessProfile.model_validate(draft)
    except ValueError:
        errors.add("form", "error.invalid_input")
        return None


def check_photos(files: Sequence[bytes]) -> str | None:
    """Check photos before anything is created; returns an i18n error key or None."""
    for data in files:
        try:
            check_image(data)
        except UploadRejected as exc:
            return exc.key
    return None


def confirm_intake(
    engine: Engine,
    settings: Settings,
    intake_id: int,
    profile: BusinessProfile,
    address: str,
    photos: Sequence[bytes] = (),
) -> Confirmed:
    """Create the business, profile version 1, consent, photos, corrections and owner access.

    The intake is claimed first (ready -> confirming in one statement), so a double tap creates
    one business; the second gets "already confirmed". If another business took the web
    address meanwhile, the intake goes back to ready and the owner is asked for another address.
    """
    with Session(engine) as session:
        claimed = session.exec(
            update(IntakeSession)
            .where(col(IntakeSession.id) == intake_id, col(IntakeSession.status) == "ready")
            .values(status="confirming")
        )
        session.commit()
        intake = session.get(IntakeSession, intake_id)
        if intake is None:
            raise AppError("error.not_found", status_code=404)
        if not claimed.rowcount:
            raise AppError("intake.already_confirmed", status_code=409)
        try:
            return _create_business(session, settings, intake, profile, address, photos)
        except IntegrityError as exc:
            session.rollback()
            _release(engine, intake_id)
            raise AppError("intake.address_taken", status_code=409) from exc


def _release(engine: Engine, intake_id: int) -> None:
    with Session(engine) as session:
        session.exec(
            update(IntakeSession)
            .where(col(IntakeSession.id) == intake_id, col(IntakeSession.status) == "confirming")
            .values(status="ready")
        )
        session.commit()


def _create_business(
    session: Session,
    settings: Settings,
    intake: IntakeSession,
    profile: BusinessProfile,
    address: str,
    photos: Sequence[bytes],
) -> Confirmed:
    """Business, profile version 1, consent, usage links, photos and owner access."""
    intake_id = intake.id
    confirmed = profile.model_dump(mode="json")
    business = Business(
        slug=address,
        display_name=profile.business_name,
        status="draft",
        # Intakes from the built-in sample voice notes are demos, never real businesses.
        is_sample=bool(intake.stt_route and intake.stt_route.startswith("sample:")),
        business_type=profile.business_type,
        owner_language=intake.language,
        consent_given_at=intake.created_at,
        consent_method="intake_checkbox",
        consent_text=intake.consent_text,
    )
    access = issue_access(business)
    session.add(business)
    session.commit()
    session.refresh(business)
    assert business.id is not None  # noqa: S101
    session.add(
        ProfileVersion(
            business_id=business.id,
            version=1,
            profile=confirmed,
            created_by="owner",
            note="confirmed at intake",
        )
    )
    intake.corrections = corrections(intake.first_guess or {}, confirmed)
    intake.draft, intake.business_id = confirmed, business.id
    intake.status, intake.completed_at = "done", utcnow()
    session.add(intake)
    for table in (ClaudeUsage, SttUsage):
        session.exec(
            update(table).where(col(table.intake_id) == intake_id).values(business_id=business.id)
        )
    session.commit()
    for data in photos:
        add_photo(session, settings, business.id, data)
    session.refresh(business)
    logger.info(
        "intake_confirmed",
        extra={
            "intake_id": intake_id,
            "business_id": business.id,
            "corrections": len(intake.corrections or []),
        },
    )
    return Confirmed(business, access)
