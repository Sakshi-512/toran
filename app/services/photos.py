"""Owner photos: checked and re-encoded (no location data), stored under UPLOADS_DIR."""

import secrets
from pathlib import Path

from sqlmodel import Session, col, select

from app.config import Settings
from app.models import Photo
from app.services.errors import AppError
from app.services.uploads import check_image

MAX_PHOTOS = 8
_EXT = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp"}


def photo_dir(settings: Settings, business_id: int) -> Path:
    """Folder for one business's photos."""
    return settings.uploads_dir / "business" / str(business_id) / "photos"


def photo_path(settings: Settings, photo: Photo) -> Path:
    """Where a photo's file is stored."""
    return photo_dir(settings, photo.business_id) / photo.filename


def list_photos(session: Session, business_id: int) -> list[Photo]:
    """A business's photos, main photo first."""
    statement = (
        select(Photo)
        .where(Photo.business_id == business_id)
        .order_by(col(Photo.is_main).desc(), col(Photo.id))
    )
    return list(session.exec(statement))


def add_photo(
    session: Session, settings: Settings, business_id: int, data: bytes, subject: str | None = None
) -> Photo:
    """Check, re-encode and store one photo. The first photo becomes the main photo."""
    existing = list_photos(session, business_id)
    if len(existing) >= MAX_PHOTOS:
        raise AppError("photos.too_many", status_code=422)
    checked = check_image(data)
    filename = f"{secrets.token_hex(8)}{_EXT[checked.mime]}"
    folder = photo_dir(settings, business_id)
    folder.mkdir(parents=True, exist_ok=True)
    (folder / filename).write_bytes(checked.data)
    photo = Photo(
        business_id=business_id, filename=filename, subject=subject or None, is_main=not existing
    )
    session.add(photo)
    session.commit()
    session.refresh(photo)
    return photo


def set_main(session: Session, business_id: int, photo_id: int) -> None:
    """Make one photo the main photo."""
    for photo in list_photos(session, business_id):
        photo.is_main = photo.id == photo_id
        session.add(photo)
    session.commit()


def set_subject(session: Session, business_id: int, photo_id: int, subject: str | None) -> None:
    """Say which product a photo shows (None means the business itself)."""
    photo = session.get(Photo, photo_id)
    if photo is None or photo.business_id != business_id:
        raise AppError("error.not_found", status_code=404)
    photo.subject = subject or None
    session.add(photo)
    session.commit()


def delete_photo(session: Session, settings: Settings, business_id: int, photo_id: int) -> None:
    """Delete a photo and its file; another photo becomes main if needed."""
    photo = session.get(Photo, photo_id)
    if photo is None or photo.business_id != business_id:
        raise AppError("error.not_found", status_code=404)
    photo_path(settings, photo).unlink(missing_ok=True)
    was_main = photo.is_main
    session.delete(photo)
    session.commit()
    remaining = list_photos(session, business_id)
    if was_main and remaining and remaining[0].id is not None:
        set_main(session, business_id, remaining[0].id)
