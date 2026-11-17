"""My details: the owner changes her phone, area or ordering policy after confirming.

Only these three fields (never products or prices). Each save is a new profile version, so
undo still works, and the draft is rebuilt and tested again. A new phone number decides where
her customers' orders go, so changing it needs her PIN (checked by the route). Her text is
data: it only fills these fields, and the same checks as the confirmation cards apply (phone
format, no address).
"""

from dataclasses import dataclass

from pydantic import ValidationError
from sqlalchemy.engine import Engine
from sqlmodel import Session

from app.config import Settings
from app.models import Business, ProfileVersion
from app.schemas import BusinessProfile, normalise_phone
from app.services import publish as pub
from app.services.address_guard import address_reason
from app.services.errors import AppError
from app.services.score_runs import queue_score

MAX_AREA = 120
MAX_POLICY = 500


@dataclass(frozen=True)
class Details:
    """What the form sends."""

    phone: str
    area: str
    policy: str
    no_order_button: bool


def current(session: Session, business_id: int) -> BusinessProfile:
    """Her latest confirmed profile."""
    version = pub.latest_profile(session, business_id)
    if version is None:
        raise AppError("error.not_found", status_code=404)
    return BusinessProfile.model_validate(version.profile)


def check(details: Details) -> dict[str, str]:
    """Field errors (i18n keys), empty when everything is fine."""
    errors: dict[str, str] = {}
    if details.phone:
        try:
            normalise_phone(details.phone)
        except ValueError:
            errors["phone"] = "intake.phone_invalid"
    if len(details.area) > MAX_AREA or (details.area and address_reason(details.area)):
        errors["area"] = "details.area_invalid"
    if len(details.policy) > MAX_POLICY:
        errors["policy"] = "agent.answer_long"
    elif details.policy and address_reason(details.policy):
        errors["policy"] = "agent.answer_address"
    return errors


def phone_changes(engine: Engine, business_id: int, phone: str) -> bool:
    """True if saving this phone number would change where her customers' orders go."""
    with Session(engine) as session:
        old = current(session, business_id).phone
    return _comparable(phone) != _comparable(old)


def _comparable(phone: str) -> str:
    if not phone:
        return ""
    try:
        return normalise_phone(phone)
    except ValueError:
        return phone


def save(engine: Engine, settings: Settings, business_id: int, details: Details) -> bool:
    """Save a new profile version if anything changed; rebuild and re-test the draft."""
    with Session(engine) as session:
        business = session.get(Business, business_id)
        latest = pub.latest_profile(session, business_id)
        if business is None or latest is None:
            raise AppError("error.not_found", status_code=404)
        data = dict(latest.profile)
        data["phone"] = details.phone
        data["area"] = details.area
        data["policy"] = details.policy
        if details.phone:
            if not data.get("order_methods"):
                data["order_methods"] = ["whatsapp"]
        elif details.no_order_button:
            data["order_methods"] = []
        elif not data.get("order_methods"):
            data["order_methods"] = ["whatsapp"]
        try:
            profile = BusinessProfile.model_validate(data)
        except ValidationError as exc:
            raise AppError("error.invalid_input", status_code=422) from exc
        if profile.model_dump() == BusinessProfile.model_validate(latest.profile).model_dump():
            return False
        session.add(
            ProfileVersion(
                business_id=business_id,
                version=latest.version + 1,
                profile=profile.model_dump(),
                created_by="owner",
                note="my details: phone, area or policy",
            )
        )
        session.commit()
    pub.build_draft(engine, settings, business_id)
    queue_score(engine, business_id, "draft")
    return True
