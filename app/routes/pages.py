"""Public pages on the main domain."""

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse
from starlette.responses import Response

from app.config import get_settings
from app.db import get_engine
from app.services.contact import submit_contact
from app.services.i18n import normalise_language
from app.services.ratelimit import CONTACT_HOURLY_LIMIT, FORM_LIMIT, rate_limit
from app.services.security import verify_csrf
from app.templating import render

router = APIRouter()


@router.get("/", include_in_schema=False)
def home(request: Request) -> Response:
    """Home page: what the product does, in the visitor's language."""
    chosen = normalise_language(request.query_params.get("lang"))
    if chosen:
        request.session["lang"] = chosen
    return render(request, "app/home.html", {"sent": request.query_params.get("sent") == "1"})


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
    dependencies=[
        Depends(rate_limit(FORM_LIMIT)),
        Depends(rate_limit(CONTACT_HOURLY_LIMIT)),
        Depends(verify_csrf),
    ],
)
async def contact(request: Request) -> RedirectResponse:
    """Contact form: save the message, queue the email, show the thank-you message."""
    form = await request.form()
    submit_contact(get_engine(), {k: v for k, v in form.items() if isinstance(v, str)})
    return RedirectResponse("/?sent=1#contact", status_code=303)
