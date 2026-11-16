"""Owner account actions that need her PIN again: deleting her site and data.

Deleting is permanent. She sees a warning and the full list of what goes, enters her PIN again
(require_recent_pin), and presses a final button. The site stops being served at once, she is
signed out everywhere on this device, and the team mailbox gets a note with no personal details.
"""

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse
from sqlmodel import Session
from starlette.responses import Response

from app.config import get_settings
from app.db import get_engine
from app.models import Business, utcnow
from app.services import publish as pub
from app.services.auth import (
    check_pin,
    forget_device,
    require_owner,
    require_recent_pin,
    sign_in,
    sign_out,
)
from app.services.errors import AppError
from app.services.mail_jobs import queue_team_email
from app.services.ratelimit import FORM_LIMIT, RateLimited, check
from app.services.security import verify_csrf
from app.templating import render

router = APIRouter(prefix="/owner")
NEXT_PAGES = {"delete": "/owner/delete"}


def _business(session: Session, business_id: int) -> Business:
    business = session.get(Business, business_id)
    if business is None or business.deleted_at is not None:
        raise AppError("error.not_found", status_code=404)
    return business


def _has_recent_pin(request: Request) -> bool:
    try:
        require_recent_pin(request)
    except AppError:
        return False
    return True


@router.get("/pin", include_in_schema=False)
def pin_again(request: Request, business_id: int = Depends(require_owner)) -> Response:
    """Enter the PIN again before a sensitive change."""
    with Session(get_engine()) as session:
        business = _business(session, business_id)
    after = request.query_params.get("next", "delete")
    return render(
        request,
        "app/owner_pin_again.html",
        {"business": business, "next": after if after in NEXT_PAGES else "delete"},
    )


@router.post("/pin", include_in_schema=False, dependencies=[Depends(verify_csrf)])
async def pin_again_submit(request: Request, business_id: int = Depends(require_owner)) -> Response:
    """Check the PIN (same lockout as sign-in), then continue to the change she asked for."""
    form = await request.form()
    after = str(form.get("next", "delete"))
    after = after if after in NEXT_PAGES else "delete"
    with Session(get_engine()) as session:
        business = _business(session, business_id)
        context = {"business": business, "next": after}
        try:
            check(request, FORM_LIMIT)
        except RateLimited as exc:
            return render(
                request,
                "app/owner_pin_again.html",
                {**context, "wait_seconds": exc.retry_after},
                429,
            )
        result = check_pin(session, business, str(form.get("pin", "")).strip())
        session.refresh(business)
        session.expunge(business)
    if result == "ok":
        sign_in(request, business_id)
        return RedirectResponse(NEXT_PAGES[after], status_code=303)
    key = "owner.pin_locked" if result == "locked" else "owner.pin_wrong"
    status = 429 if result == "locked" else 401
    return render(request, "app/owner_pin_again.html", {**context, "error_key": key}, status)


@router.get("/delete", include_in_schema=False)
def delete_page(request: Request, business_id: int = Depends(require_owner)) -> Response:
    """The confirmation screen: a warning, everything that will be deleted, the final button."""
    if not _has_recent_pin(request):
        return RedirectResponse("/owner/pin?next=delete", status_code=303)
    with Session(get_engine()) as session:
        business = _business(session, business_id)
    return render(request, "app/owner_delete.html", {"business": business})


@router.post("/delete", include_in_schema=False, dependencies=[Depends(verify_csrf)])
def delete_confirmed(request: Request) -> Response:
    """Her final yes: delete everything now, sign her out and say goodbye."""
    business_id = require_recent_pin(request)
    pub.delete_business_data(get_engine(), get_settings(), business_id)
    queue_team_email(
        get_engine(), {"team": "deletion", "at": utcnow().isoformat(timespec="minutes")}
    )
    sign_out(request)
    response = render(request, "app/owner_goodbye.html", {})
    forget_device(request, response)
    return response
