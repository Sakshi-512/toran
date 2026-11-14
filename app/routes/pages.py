"""Public pages on the main domain."""

from fastapi import APIRouter, Request
from starlette.responses import Response

from app.config import get_settings
from app.services.i18n import normalise_language
from app.templating import render

router = APIRouter()


@router.get("/", include_in_schema=False)
def home(request: Request) -> Response:
    """Home page: what the product does, in the visitor's language."""
    chosen = normalise_language(request.query_params.get("lang"))
    if chosen:
        request.session["lang"] = chosen
    return render(request, "app/home.html")


@router.get("/privacy", include_in_schema=False)
def privacy(request: Request) -> Response:
    """Privacy page: what we store, why, how long, and how to delete it."""
    chosen = normalise_language(request.query_params.get("lang"))
    if chosen:
        request.session["lang"] = chosen
    return render(request, "app/privacy.html", {"contact_email": get_settings().contact_to_email})
