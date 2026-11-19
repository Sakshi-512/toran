"""Edit my menu and prices: the confirmation cards for products, on the owner page.

Saving changes the draft only; the live site changes when the owner publishes again. Large price
changes need the PIN again (see app/services/menu.PIN_RULES).
"""

from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse
from sqlmodel import Session
from starlette.responses import Response

from app.config import get_settings
from app.db import get_engine
from app.models import Business
from app.routes.owner_details import pin_problem
from app.services import menu as svc
from app.services.auth import require_owner
from app.services.errors import AppError
from app.services.intake import CardErrors
from app.services.security import verify_csrf
from app.templating import render

router = APIRouter(prefix="/owner")
IST = ZoneInfo("Asia/Kolkata")
NEW_OPTION = {"label": "", "price": ""}


def _when(moment: datetime) -> str:
    """A date and time the owner reads the same in any language (India time)."""
    aware = moment if moment.tzinfo else moment.replace(tzinfo=UTC)
    return f"{aware.astimezone(IST):%d/%m/%Y, %H:%M}"


def _page(
    request: Request,
    business_id: int,
    products: list[dict[str, Any]] | None = None,
    **extra: object,
) -> dict[str, object]:
    with Session(get_engine()) as session:
        busy = svc.busy(session, business_id)  # may close a stale agent run, so read it first
        business = session.get(Business, business_id)
        if business is None or business.deleted_at is not None:
            raise AppError("error.not_found", status_code=404)
        shown = products if products is not None else svc.current_products(session, business_id)
        before = svc.previous(session, business_id)
    context: dict[str, object] = {
        "business": business,
        "draft": {"products": shown},
        "errors": {},
        "from_photo": False,
        "busy": busy,
        "previous": before,
        "previous_when": _when(before.created_at) if before else "",
        **extra,
    }
    return context


def _show(
    request: Request,
    business_id: int,
    products: list[dict[str, Any]] | None = None,
    status_code: int = 200,
    **extra: object,
) -> Response:
    context = _page(request, business_id, products, **extra)
    return render(request, "app/owner_menu.html", context, status_code)


@router.get("/menu", include_in_schema=False)
def menu_page(request: Request, business_id: int = Depends(require_owner)) -> Response:
    """The owner's products and prices as editable cards."""
    done = request.query_params.get("done", "")
    return _show(request, business_id, done=done if done in ("saved", "restored", "same") else "")


@router.post("/menu", include_in_schema=False, dependencies=[Depends(verify_csrf)])
async def menu_save(request: Request, business_id: int = Depends(require_owner)) -> Response:
    """Add a card or an option row, or check and save (PIN again for large price changes)."""
    form = await request.form()
    errors = CardErrors()
    products = svc.products_from_form(form, errors)
    action = str(form.get("action", "save"))
    if action == "add_product":
        products.append({"name": "", "unit": "piece", "options": [dict(NEW_OPTION)], "notes": ""})
        return _show(request, business_id, products, new_row=f"p{len(products) - 1}")
    if action.startswith("add_option:") and action[11:].isdigit():
        index = int(action[11:])
        if index < len(products):
            products[index]["options"].append(dict(NEW_OPTION))
            new_row = f"p{index}_o{len(products[index]['options']) - 1}"
            return _show(request, business_id, products, new_row=new_row)
        return _show(request, business_id, products)
    if errors.errors:
        return _show(request, business_id, products, 422, errors=errors.errors)
    with Session(get_engine()) as session:
        if svc.busy(session, business_id):
            return _show(request, business_id, products, 409, errors={"form": "menu.busy"})
        large = svc.needs_pin(svc.baseline_products(session, business_id), products)
    if large:
        problem = pin_problem(
            request, business_id, str(form.get("pin", "")).strip(), "menu.pin_needed"
        )
        if problem:
            return _show(request, business_id, products, 401, errors={"pin": problem}, ask_pin=True)
    try:
        changed = svc.save(get_engine(), get_settings(), business_id, products)
    except AppError as exc:
        if exc.key not in ("menu.busy", "menu.invalid"):
            raise
        return _show(request, business_id, products, exc.status_code, errors={"form": exc.key})
    return RedirectResponse(f"/owner/menu?done={'saved' if changed else 'same'}", status_code=303)


@router.post("/menu/restore", include_in_schema=False, dependencies=[Depends(verify_csrf)])
def menu_restore(request: Request, business_id: int = Depends(require_owner)) -> Response:
    """Make the previous version's products current again (a new version)."""
    try:
        changed = svc.restore_previous(get_engine(), get_settings(), business_id)
    except AppError as exc:
        if exc.key not in ("menu.busy", "menu.nothing_to_restore"):
            raise
        return _show(request, business_id, None, exc.status_code, errors={"form": exc.key})
    return RedirectResponse(
        f"/owner/menu?done={'restored' if changed else 'same'}", status_code=303
    )
