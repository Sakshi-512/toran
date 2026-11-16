"""My details page: phone, area and ordering policy (no products or prices)."""

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse
from sqlmodel import Session
from starlette.responses import Response

from app.config import get_settings
from app.db import get_engine
from app.models import Business
from app.services import details as svc
from app.services.auth import require_owner
from app.services.errors import AppError
from app.services.security import verify_csrf
from app.templating import render

router = APIRouter(prefix="/owner")


def _page(request: Request, business_id: int, **extra: object) -> dict[str, object]:
    with Session(get_engine()) as session:
        business = session.get(Business, business_id)
        if business is None or business.deleted_at is not None:
            raise AppError("error.not_found", status_code=404)
        profile = svc.current(session, business_id)
    return {"business": business, "profile": profile, "errors": {}, **extra}


@router.get("/details", include_in_schema=False)
def details_page(request: Request, business_id: int = Depends(require_owner)) -> Response:
    """Her phone, area and ordering policy."""
    saved = request.query_params.get("saved") == "1"
    return render(request, "app/owner_details.html", _page(request, business_id, saved=saved))


@router.post("/details", include_in_schema=False, dependencies=[Depends(verify_csrf)])
async def details_save(request: Request, business_id: int = Depends(require_owner)) -> Response:
    """Check and save; the draft is rebuilt and tested again."""
    form = await request.form()
    sent = svc.Details(
        phone=str(form.get("phone", "")).strip(),
        area=" ".join(str(form.get("area", "")).split()),
        policy=" ".join(str(form.get("policy", "")).split()),
        no_order_button=form.get("no_order_button") == "yes",
    )
    errors = svc.check(sent)
    if errors:
        context = _page(request, business_id, errors=errors, sent=sent)
        return render(request, "app/owner_details.html", context, 422)
    svc.save(get_engine(), get_settings(), business_id, sent)
    return RedirectResponse("/owner/details?saved=1", status_code=303)
