"""Owner intake pages: start, wait, confirmation cards, done. Logic lives in services.intake."""

from collections.abc import Sequence

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse
from sqlmodel import Session
from starlette.datastructures import UploadFile
from starlette.responses import Response

from app.config import get_settings
from app.db import get_engine
from app.services import intake as svc
from app.services.errors import AppError
from app.services.i18n import normalise_language, pick_language
from app.services.jobs import get_job
from app.services.publish import build_draft
from app.services.ratelimit import (
    FORM_LIMIT,
    INTAKE_DAILY_LIMIT,
    INTAKE_HOURLY_LIMIT,
    RateLimited,
    check,
)
from app.services.score_runs import queue_score
from app.services.security import verify_csrf
from app.services.sites import main_origin
from app.services.stt import STT_SAMPLES
from app.templating import render

router = APIRouter(prefix="/start")
SOURCE_OF_SAMPLE = {"sample-en": "en", "sample-hi": "hi", "sample-mr": "mr"}


START_OPTIONS = ("record", "voice", "photo", "type")


async def _file_bytes(value: object) -> bytes:
    return await value.read() if isinstance(value, UploadFile) else b""


async def _first_file(values: Sequence[object]) -> bytes:
    """The first non-empty upload (the voice and photo cards each have a file field)."""
    for value in values:
        if isinstance(value, UploadFile):
            data = await value.read()
            if data:
                return data
    return b""


@router.get("", include_in_schema=False)
def start_page(request: Request) -> Response:
    """Choose how to tell us: record, upload a voice note or photo, or type."""
    chosen = normalise_language(request.query_params.get("lang"))
    if chosen:
        request.session["lang"] = chosen
    option = request.query_params.get("option", "")
    return render(
        request,
        "app/start.html",
        {"samples": STT_SAMPLES, "option": option if option in START_OPTIONS else ""},
    )


@router.post(
    "",
    include_in_schema=False,
    dependencies=[Depends(verify_csrf)],
)
async def start_submit(request: Request) -> Response:
    """Save the input and queue processing, then show the waiting page."""
    try:
        check(request, FORM_LIMIT, INTAKE_HOURLY_LIMIT, INTAKE_DAILY_LIMIT)
    except RateLimited as exc:
        return render(
            request,
            "app/start.html",
            {"samples": STT_SAMPLES, "wait_seconds": exc.retry_after},
            status_code=429,
        )
    form = await request.form()
    settings, engine = get_settings(), get_engine()
    sample = str(form.get("sample", ""))
    picked = str(form.get("language", ""))
    language = picked or pick_language(request)
    # Only a language she chose is kept for the rest of her journey. A browser guess or a
    # sample's language (a judge may try the Marathi sample from the English page) is not.
    chosen = normalise_language(picked)
    if chosen:
        request.session["lang"] = chosen
    try:
        if sample in SOURCE_OF_SAMPLE:
            started = svc.start_sample(engine, settings, sample, SOURCE_OF_SAMPLE[sample])
        else:
            started = svc.start_intake(
                engine,
                settings,
                source=str(form.get("source", "")),
                language=language,
                consent=form.get("consent") == "yes",
                keep_input=form.get("keep_input") == "yes",
                text=str(form.get("text", "")),
                data=await _first_file(form.getlist("upload")),
            )
    except AppError as exc:
        return render(
            request,
            "app/start.html",
            {"samples": STT_SAMPLES, "error_key": exc.key},
            status_code=exc.status_code,
        )
    return RedirectResponse(f"/start/{started.token}/wait", status_code=303)


@router.get("/{token}/wait", include_in_schema=False)
def wait_page(token: str, request: Request) -> Response:
    """Shows progress and refreshes itself until the cards are ready."""
    with Session(get_engine()) as session:
        intake = svc.intake_for_token(session, token)
        status, error_key, job_id = intake.status, intake.error_key, intake.job_id
    if status == "ready":
        return RedirectResponse(f"/start/{token}/confirm", status_code=303)
    if status == "done":
        raise AppError("error.not_found", status_code=404)
    job = get_job(get_engine(), job_id) if job_id else None
    if job is not None and job.status == "failed" and status != "failed":
        status, error_key = "failed", "intake.extract_failed"
    return render(request, "app/intake_wait.html", {"status": status, "error_key": error_key})


def _already_created(request: Request) -> Response:
    """A second Confirm (a double tap, or the back button): the business exists and her link
    and PIN were shown once. Explain that, and how to get new ones from the team."""
    response = render(request, "app/intake_already.html", {}, status_code=409)
    response.headers["Cache-Control"] = "no-store"
    return response


def _cards(
    request: Request,
    token: str,
    draft: dict[str, object],
    address: str,
    source: str,
    errors: dict[str, str] | None = None,
    status_code: int = 200,
    sample: bool = False,
) -> Response:
    return render(
        request,
        "app/intake_confirm.html",
        {
            "token": token,
            "draft": draft,
            "address": address,
            "from_photo": source == "photo",
            "errors": errors or {},
            "photo_error": (errors or {}).get("photos"),
            "sample": sample,
        },
        status_code=status_code,
    )


@router.get("/{token}/confirm", include_in_schema=False)
def confirm_page(token: str, request: Request) -> Response:
    """One card per detail, plus her questions."""
    with Session(get_engine()) as session:
        intake = svc.intake_for_token(session, token)
        if intake.status != "ready" or intake.draft is None:
            return RedirectResponse(f"/start/{token}/wait", status_code=303)
        return _cards(
            request,
            token,
            intake.draft,
            intake.web_address or "",
            intake.source,
            sample=svc.is_sample_intake(intake),
        )


@router.post("/{token}/confirm", include_in_schema=False, dependencies=[Depends(verify_csrf)])
async def confirm_submit(token: str, request: Request) -> Response:
    """Save edits, add rows, or confirm and create the business."""
    form = await request.form()
    multi = {
        key: [str(v) for v in form.getlist(key)]
        for key in ("languages", "order_methods", "missing")
    }
    action = str(form.get("action", "save"))
    errors = svc.CardErrors()
    draft = svc.draft_from_form(form, multi, errors)
    address = str(form.get("web_address", "")).strip().lower()
    engine, settings = get_engine(), get_settings()
    with Session(engine) as session:
        intake = svc.intake_for_token(session, token)
        if intake.status in ("confirming", "done"):
            return _already_created(request)
        if intake.status != "ready":
            raise AppError("error.not_found", status_code=404)
        source = intake.source
        sample = svc.is_sample_intake(intake)
        if action == "add_product":
            draft["products"].append(
                {"name": "", "unit": "piece", "options": [{"label": "", "price": ""}], "notes": ""}
            )
        elif action.startswith("add_option:") and action[11:].isdigit():
            index = int(action[11:])
            if index < len(draft["products"]):
                draft["products"][index]["options"].append({"label": "", "price": ""})
        intake.draft, intake.web_address = draft, address
        session.add(intake)
        session.commit()
        if action != "confirm":
            return _cards(request, token, draft, address, source, sample=sample)
        profile = svc.validate_draft(session, dict(draft), address, errors)
        photos = [await _file_bytes(f) for f in form.getlist("photos")]
        photos = [p for p in photos if p]
        photo_error = svc.check_photos(photos)
        if photo_error:
            errors.add("photos", photo_error)
        if profile is None or errors.errors:
            return _cards(request, token, draft, address, source, errors.errors, 422, sample)
        intake_id = intake.id
    assert intake_id is not None  # noqa: S101
    try:
        confirmed = svc.confirm_intake(engine, settings, intake_id, profile, address, photos[:8])
    except AppError as exc:
        if exc.key == "intake.already_confirmed":
            return _already_created(request)
        if exc.key != "intake.address_taken":
            raise
        errors.add("web_address", exc.key)
        return _cards(request, token, draft, address, source, errors.errors, 422, sample)
    assert confirmed.business.id is not None  # noqa: S101
    build_draft(engine, settings, confirmed.business.id)  # warm style by default
    queue_score(engine, confirmed.business.id, "draft")
    link = f"{main_origin(settings)}/o/{confirmed.access.link_token}"
    response = render(
        request,
        "app/intake_done.html",
        {
            "link": link,
            "pin": confirmed.access.pin,
            "business_name": confirmed.business.display_name,
        },
    )
    response.headers["Cache-Control"] = "no-store"  # the PIN is shown once; never cache it
    return response
