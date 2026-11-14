"""Enquiry form endpoint for business sites."""

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse
from sqlmodel import Session

from app.config import get_settings
from app.db import get_session
from app.services.enquiries import submit_enquiry
from app.services.ratelimit import FORM_LIMIT, rate_limit

router = APIRouter()


@router.post("/api/enquiry/{slug}", dependencies=[Depends(rate_limit(FORM_LIMIT))])
async def enquiry(
    slug: str, request: Request, session: Session = Depends(get_session)
) -> RedirectResponse:
    """Save an enquiry sent from a business site, then show its thank-you page."""
    form = await request.form()
    fields = {k: v for k, v in form.items() if isinstance(v, str)}
    thanks_url = submit_enquiry(
        session, get_settings(), slug, request.headers.get("origin"), fields
    )
    return RedirectResponse(thanks_url, status_code=303)
