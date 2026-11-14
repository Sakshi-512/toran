"""Public pages on the main domain."""

from fastapi import APIRouter, Request
from starlette.responses import Response

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
