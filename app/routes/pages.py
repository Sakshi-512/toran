"""Public pages on the main domain."""

from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse
from sqlmodel import Session
from starlette.responses import Response

from app.config import get_settings
from app.db import get_engine
from app.models import Business
from app.services import showcase
from app.services.agent_report import agent_view
from app.services.contact import submit_contact
from app.services.errors import AppError, FieldError
from app.services.i18n import normalise_language, pick_language
from app.services.ratelimit import CONTACT_HOURLY_LIMIT, FORM_LIMIT, RateLimited, check
from app.services.security import verify_csrf
from app.services.sites import site_origin
from app.services.stt import STT_SAMPLES
from app.templating import render

router = APIRouter()


HERO_SHOT = "home/hero-sample.webp"  # made by `make shots` from this sample
HERO_SLUG = "asha-home-bakery"


def home_context(request: Request) -> dict[str, Any]:
    """Everything the home page shows, from real data only."""
    lang = pick_language(request)
    with Session(get_engine()) as session:
        run = showcase.latest_demo_run(session)
        before_after = agent_view(session, run, lang) if run else None
        demo_name = ""
        if run is not None:
            business = session.get(Business, run.business_id)
            demo_name = business.display_name if business else ""
        real, demo = showcase.gallery(session)
        stats = showcase.home_stats(session)
    return {
        "stats": stats,
        "before_after": before_after,
        "demo_name": demo_name,
        "real_businesses": real,
        "demo_businesses": demo,
        "samples": STT_SAMPLES,
        "hero_shot": HERO_SHOT,
        "hero_url": site_url(HERO_SLUG) if any(d.slug == HERO_SLUG for d in demo) else "",
        "site_url": site_url,
    }


def site_url(slug: str) -> str:
    """A business site's public address."""
    return f"{site_origin(slug, get_settings())}/"


@router.get("/", include_in_schema=False)
def home(request: Request) -> Response:
    """Home page: what the product does, in the visitor's language, and how to try it."""
    chosen = normalise_language(request.query_params.get("lang"))
    if chosen:
        request.session["lang"] = chosen
    context = home_context(request)
    context["sent"] = request.query_params.get("sent") == "1"
    return render(request, "app/home.html", context)


@router.get("/gallery/{slug}.webp", include_in_schema=False)
def gallery_thumbnail(slug: str) -> Response:
    """A small picture of a gallery business's site (only businesses shown in the gallery)."""
    with Session(get_engine()) as session:
        data = showcase.thumbnail(session, get_settings(), slug)
    if data is None:
        raise AppError("error.not_found", status_code=404)
    return Response(
        data, media_type="image/webp", headers={"Cache-Control": "public, max-age=3600"}
    )


@router.get("/demo/agent/{run_id}", include_in_schema=False)
def demo_agent(run_id: str, request: Request) -> Response:
    """Read-only "how it thinks" for demo businesses (all sample data, real Claude runs)."""
    with Session(get_engine()) as session:
        run = showcase.public_agent_run(session, run_id)
        if run is None:
            raise AppError("error.not_found", status_code=404)
        business = session.get(Business, run.business_id)
        view = agent_view(session, run, pick_language(request))
    return render(
        request, "app/owner_agent.html", {"business": business, "agent": view, "public": True}
    )


@router.get("/privacy", include_in_schema=False)
def privacy(request: Request) -> Response:
    """Privacy page: what we store, why, how long, and how to delete it."""
    chosen = normalise_language(request.query_params.get("lang"))
    if chosen:
        request.session["lang"] = chosen
    return render(request, "app/privacy.html", {"contact_email": get_settings().contact_to_email})


@router.post(
    "/contact",
    include_in_schema=False,
    dependencies=[Depends(verify_csrf)],
)
async def contact(request: Request) -> Response:
    """Contact form: save the message, queue the email, show the thank-you message."""
    try:
        check(request, FORM_LIMIT, CONTACT_HOURLY_LIMIT)
    except RateLimited as exc:
        context = {**home_context(request), "wait_seconds": exc.retry_after}
        return render(request, "app/home.html", context, status_code=429)
    form = await request.form()
    fields = {k: v for k, v in form.items() if isinstance(v, str)}
    try:
        submit_contact(get_engine(), fields)
    except FieldError as exc:
        # Show the home page again with the message next to the field and the typed text kept.
        sent = {k: fields.get(k, "") for k in ("name", "contact", "message")}
        context = {
            **home_context(request),
            "contact_errors": {exc.field: exc.key},
            "contact_sent": sent,
        }
        return render(request, "app/home.html", context, status_code=exc.status_code)
    return RedirectResponse("/?sent=1#contact", status_code=303)
