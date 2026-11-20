"""The contact form on the main site: validate, save, and queue the email to our mailbox."""

import logging
from collections.abc import Mapping

from sqlalchemy.engine import Engine
from sqlmodel import Session

from app.models import Enquiry
from app.schemas import normalise_phone
from app.services.errors import FieldError
from app.services.mail_jobs import queue_notification
from app.services.mailer import clean_header, valid_email
from app.services.security import is_spam

logger = logging.getLogger(__name__)

MAX_NAME = 120
MAX_CONTACT = 254
MAX_MESSAGE = 2000


# The message for each field when it is empty or too long (the rules are unchanged).
FIELD_MESSAGES = {
    "name": "contact.name_invalid",
    "contact": "contact.bad_contact",
    "message": "contact.message_invalid",
}


def _field(form: Mapping[str, str], key: str, limit: int) -> str:
    value = str(form.get(key, "")).strip()
    if not value or len(value) > limit:
        raise FieldError(FIELD_MESSAGES[key], key)
    return value


def _email_or_phone(value: str) -> tuple[str | None, str | None]:
    if "@" in value:
        if not valid_email(value):
            raise FieldError("contact.bad_contact", "contact")
        return value, None
    try:
        return None, normalise_phone(value)
    except ValueError as exc:
        raise FieldError("contact.bad_contact", "contact") from exc


def submit_contact(engine: Engine, form: Mapping[str, str]) -> int | None:
    """Save a contact message and queue its email. Spam-trap posts are dropped (returns None)."""
    if is_spam(dict(form)):
        logger.info("contact_spam_dropped")
        return None
    name = clean_header(_field(form, "name", MAX_NAME), MAX_NAME)
    email, phone = _email_or_phone(_field(form, "contact", MAX_CONTACT))
    message = _field(form, "message", MAX_MESSAGE)
    with Session(engine) as session:
        enquiry = Enquiry(business_id=None, name=name, email=email, phone=phone, message=message)
        session.add(enquiry)
        session.commit()
        assert enquiry.id is not None  # noqa: S101
        enquiry_id = enquiry.id
    queue_notification(engine, enquiry_id)
    logger.info("contact_saved", extra={"enquiry_id": enquiry_id})
    return enquiry_id
