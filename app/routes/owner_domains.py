"""Owner pages for web address suggestions (GoDaddy). Nothing is bought here."""

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse
from starlette.responses import Response

from app.db import get_engine
from app.services import godaddy
from app.services.auth import require_owner
from app.services.errors import AppError
from app.services.ratelimit import FORM_LIMIT, RateLimited, check
from app.services.security import verify_csrf

router = APIRouter(prefix="/owner")
ERRORS = {
    "domains.too_many": "too_many",
    "domains.not_offered": "not_offered",
    "domains.already_requested": "already",
}


def _back(problem: str | None = None) -> RedirectResponse:
    query = f"?domains_error={problem}" if problem else ""
    return RedirectResponse(f"/owner{query}#domains", status_code=303)


@router.post("/domains", include_in_schema=False, dependencies=[Depends(verify_csrf)])
def find(request: Request, business_id: int = Depends(require_owner)) -> Response:
    """The owner's "Show me web addresses" button: a background lookup (cached for an hour)."""
    try:
        check(request, FORM_LIMIT)
        godaddy.start_lookup(get_engine(), business_id)
    except RateLimited:
        return _back("too_many")
    except AppError as exc:
        if exc.key not in ERRORS:
            raise
        return _back(ERRORS[exc.key])
    return _back()


@router.post("/domains/request", include_in_schema=False, dependencies=[Depends(verify_csrf)])
async def want(request: Request, business_id: int = Depends(require_owner)) -> Response:
    """The owner's "I want this one": recorded and emailed to the team, who buy it for the owner."""
    form = await request.form()
    try:
        check(request, FORM_LIMIT)
        godaddy.request_domain(get_engine(), business_id, str(form.get("domain", "")).strip())
    except RateLimited:
        return _back("too_many")
    except AppError as exc:
        if exc.key not in ERRORS:
            raise
        return _back(ERRORS[exc.key])
    return _back()
