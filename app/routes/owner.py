"""Owner pages: PIN entry for the private link, her business page and her photos."""

from fastapi import APIRouter, Depends, Request
from fastapi.responses import FileResponse, RedirectResponse
from sqlmodel import Session, col, select
from starlette.datastructures import UploadFile
from starlette.responses import Response

from app.config import get_settings
from app.db import get_engine
from app.models import Business, Photo, ProfileVersion
from app.routes.owner_site import owner_site_context
from app.services import photos as photo_svc
from app.services.auth import business_for_link, check_pin, require_owner, sign_in, sign_out
from app.services.errors import AppError
from app.services.i18n import pick_language
from app.services.ratelimit import FORM_LIMIT, rate_limit
from app.services.security import verify_csrf
from app.templating import render

router = APIRouter()


@router.get("/o/{token}", include_in_schema=False)
def pin_page(token: str, request: Request) -> Response:
    """PIN entry for an owner's private link."""
    with Session(get_engine()) as session:
        if business_for_link(session, token) is None:
            raise AppError("error.not_found", status_code=404)
    return render(request, "app/owner_pin.html", {"token": token})


@router.post(
    "/o/{token}",
    include_in_schema=False,
    dependencies=[Depends(rate_limit(FORM_LIMIT)), Depends(verify_csrf)],
)
async def pin_submit(token: str, request: Request) -> Response:
    """Check the PIN; five wrong tries lock the link for 15 minutes."""
    form = await request.form()
    with Session(get_engine()) as session:
        business = business_for_link(session, token)
        if business is None or business.id is None:
            raise AppError("error.not_found", status_code=404)
        result = check_pin(session, business, str(form.get("pin", "")).strip())
        business_id = business.id
    if result == "ok":
        sign_in(request, business_id)
        return RedirectResponse("/owner", status_code=303)
    key = "owner.pin_locked" if result == "locked" else "owner.pin_wrong"
    return render(
        request,
        "app/owner_pin.html",
        {"token": token, "error_key": key},
        status_code=429 if result == "locked" else 401,
    )


def _owner_context(session: Session, business_id: int, lang: str = "en") -> dict[str, object]:
    business = session.get(Business, business_id)
    if business is None or business.deleted_at is not None:
        raise AppError("error.not_found", status_code=404)
    version = session.exec(
        select(ProfileVersion)
        .where(ProfileVersion.business_id == business_id)
        .order_by(col(ProfileVersion.version).desc())
    ).first()
    profile = version.profile if version else {}
    return {
        **owner_site_context(session, business, lang),
        "business": business,
        "profile": profile,
        "photos": photo_svc.list_photos(session, business_id),
        "product_names": [p.get("name", "") for p in profile.get("products", [])],
    }


@router.get("/owner", include_in_schema=False)
def owner_home(request: Request, business_id: int = Depends(require_owner)) -> Response:
    """The signed-in owner's page."""
    with Session(get_engine()) as session:
        context = _owner_context(session, business_id, pick_language(request))
        return render(request, "app/owner_home.html", context)


@router.post("/owner/photos", include_in_schema=False, dependencies=[Depends(verify_csrf)])
async def owner_add_photos(request: Request, business_id: int = Depends(require_owner)) -> Response:
    """Add photos (checked and re-encoded)."""
    form = await request.form()
    files = [await f.read() for f in form.getlist("photos") if isinstance(f, UploadFile)]
    with Session(get_engine()) as session:
        try:
            for data in [d for d in files if d]:
                photo_svc.add_photo(session, get_settings(), business_id, data)
        except AppError as exc:
            context = _owner_context(session, business_id, pick_language(request))
            return render(
                request,
                "app/owner_home.html",
                {**context, "error_key": exc.key},
                status_code=exc.status_code,
            )
    return RedirectResponse("/owner#photos", status_code=303)


@router.post(
    "/owner/photos/{photo_id}", include_in_schema=False, dependencies=[Depends(verify_csrf)]
)
async def owner_photo_action(
    photo_id: int, request: Request, business_id: int = Depends(require_owner)
) -> Response:
    """Make main, set what it shows, or delete one photo."""
    form = await request.form()
    action = str(form.get("action", ""))
    with Session(get_engine()) as session:
        if action == "main":
            if _photo_of(session, business_id, photo_id) is not None:
                photo_svc.set_main(session, business_id, photo_id)
        elif action == "subject":
            photo_svc.set_subject(session, business_id, photo_id, str(form.get("subject", "")))
        elif action == "delete":
            photo_svc.delete_photo(session, get_settings(), business_id, photo_id)
    return RedirectResponse("/owner#photos", status_code=303)


def _photo_of(session: Session, business_id: int, photo_id: int) -> Photo | None:
    photo = session.get(Photo, photo_id)
    if photo is None or photo.business_id != business_id:
        raise AppError("error.not_found", status_code=404)
    return photo


@router.get("/owner/photos/{photo_id}", include_in_schema=False)
def owner_photo_file(photo_id: int, business_id: int = Depends(require_owner)) -> FileResponse:
    """One of the owner's own photos (never anyone else's)."""
    with Session(get_engine()) as session:
        photo = _photo_of(session, business_id, photo_id)
        assert photo is not None  # noqa: S101
        path = photo_svc.photo_path(get_settings(), photo)
    if not path.is_file():
        raise AppError("error.not_found", status_code=404)
    return FileResponse(path, headers={"Cache-Control": "private, no-store"})


@router.post("/owner/logout", include_in_schema=False, dependencies=[Depends(verify_csrf)])
def owner_logout(request: Request) -> RedirectResponse:
    """Sign out."""
    sign_out(request)
    return RedirectResponse("/", status_code=303)
