"""Customer enquiries from business sites.

The form lives on a static site and posts across origins to the main app, so a per-session CSRF
token is impossible. Instead the Origin header must exactly match the business's own site origin.
Spam-trap submissions are dropped silently. Rate limiting is applied by the route.
"""

import logging
from collections.abc import Mapping

from sqlmodel import Session

from app.config import Settings
from app.models import Enquiry
from app.schemas import normalise_phone
from app.services.errors import AppError
from app.services.i18n import normalise_language
from app.services.mail_jobs import queue_notification
from app.services.security import is_spam
from app.services.sitegen import LANG_PREFIX
from app.services.sites import active_business, allowed_enquiry_origins

logger = logging.getLogger(__name__)

MAX_NAME = 120
MAX_MESSAGE = 2000


class EnquiryRejected(AppError):
    """The enquiry did not come from the business's own site."""

    def __init__(self) -> None:
        super().__init__("error.enquiry_origin", status_code=403)


def check_origin(origin: str | None, slug: str, settings: Settings) -> str:
    """Return the origin if it exactly matches the business's site, else raise."""
    if not origin or origin not in allowed_enquiry_origins(slug, settings):
        raise EnquiryRejected()
    return origin


def _clean(form: Mapping[str, str], key: str, limit: int, required: bool) -> str:
    value = str(form.get(key, "")).strip()
    if (required and not value) or len(value) > limit:
        raise AppError("error.invalid_input", status_code=422)
    return value


def _phone(raw: str) -> str | None:
    if not raw:
        return None
    try:
        return normalise_phone(raw)
    except ValueError as exc:
        raise AppError("error.invalid_input", status_code=422) from exc


def submit_enquiry(
    session: Session,
    settings: Settings,
    slug: str,
    origin: str | None,
    form: Mapping[str, str],
) -> str:
    """Validate and save an enquiry. Returns the thank-you page URL to redirect to."""
    business = active_business(session, slug)
    if business is None:
        raise AppError("error.not_found", status_code=404)
    site = check_origin(origin, slug, settings)
    lang = normalise_language(form.get("lang")) or "en"
    thanks_url = f"{site}/{LANG_PREFIX[lang]}thanks/"
    if is_spam(dict(form)):
        logger.info("enquiry_spam_dropped", extra={"business_id": business.id})
        return thanks_url
    enquiry = Enquiry(
        business_id=business.id,
        name=_clean(form, "name", MAX_NAME, required=True),
        phone=_phone(_clean(form, "phone", 20, required=False)),
        message=_clean(form, "message", MAX_MESSAGE, required=True),
    )
    session.add(enquiry)
    session.commit()
    assert enquiry.id is not None  # noqa: S101
    queue_notification(session.get_bind(), enquiry.id)  # type: ignore[arg-type]
    logger.info("enquiry_saved", extra={"business_id": business.id, "enquiry_id": enquiry.id})
    return thanks_url
