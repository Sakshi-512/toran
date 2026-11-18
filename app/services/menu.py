"""The owner's menu and prices editor.

Uses the same card form and checks as the confirmation cards at intake. Saving keeps a new profile
version (so undo still works), rebuilds the draft and tests it again. The live site changes only
when the owner publishes again (rule 2). Facts and prices come only from what the owner typed
(rules 1 and 4), and nothing that looks like a home address is accepted (rule 3).
"""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from pydantic import ValidationError
from sqlalchemy.engine import Engine
from sqlmodel import Session, col, select

from app.config import Settings
from app.models import Job, ProfileVersion
from app.schemas import BusinessProfile
from app.services import publish as pub
from app.services.address_guard import looks_like_address
from app.services.errors import AppError
from app.services.intake import CardErrors, draft_from_form
from app.services.score_runs import SCORE_JOB, queue_score


@dataclass(frozen=True)
class PinRules:
    """When saving the menu needs the PIN again. Tune here."""

    max_changed_prices: int = 5  # more than this many prices changed at once
    max_change_percent: int = 50  # or any one price changed by more than this


PIN_RULES = PinRules()

PriceKey = tuple[str, str]


@dataclass(frozen=True)
class Previous:
    """The profile version just before the current one, as shown on the restore button."""

    version: int
    created_at: datetime
    product_count: int


def _latest(session: Session, business_id: int) -> ProfileVersion:
    pub._business(session, business_id)
    latest = pub.latest_profile(session, business_id)
    if latest is None:
        raise AppError("error.not_found", status_code=404)
    return latest


def current_products(session: Session, business_id: int) -> list[dict[str, Any]]:
    """The products in the latest saved profile, ready for the cards."""
    profile = BusinessProfile.model_validate(_latest(session, business_id).profile)
    return [p.model_dump() for p in profile.products]


def products_from_form(form: Mapping[str, Any], errors: CardErrors) -> list[dict[str, Any]]:
    """Read the product cards (same parsing and price checks as intake) and check for addresses."""
    products: list[dict[str, Any]] = draft_from_form(form, {}, errors)["products"]
    for i, product in enumerate(products):
        if looks_like_address(product["name"]):
            errors.add(f"p{i}_name", "menu.address_in_name")
        for j, option in enumerate(product["options"]):
            if looks_like_address(str(option["label"])):
                errors.add(f"p{i}_o{j}_label", "menu.address_in_label")
        if looks_like_address(product["notes"]):
            errors.add(f"p{i}_notes", "menu.address_in_notes")
    if not products:
        errors.add("products", "menu.empty")
    return products


def _prices(products: list[dict[str, Any]]) -> dict[PriceKey, int]:
    return {
        (str(p["name"]).casefold(), str(o["label"]).casefold()): int(o["price"])
        for p in products
        for o in p["options"]
    }


def needs_pin(old: list[dict[str, Any]], new: list[dict[str, Any]]) -> bool:
    """True if the change is big enough to ask for the PIN again (see PIN_RULES).

    A price counts as changed when it is new, removed or different. A big change is one price
    moving by more than max_change_percent (any change from 0 counts as big).
    """
    before, after = _prices(old), _prices(new)
    changed = [k for k in before.keys() | after.keys() if before.get(k) != after.get(k)]
    if len(changed) > PIN_RULES.max_changed_prices:
        return True
    for key in changed:
        if key in before and key in after:
            was, now = before[key], after[key]
            if was == 0 or abs(now - was) * 100 > was * PIN_RULES.max_change_percent:
                return True
    return False


def baseline_products(session: Session, business_id: int) -> list[dict[str, Any]]:
    """What changes are measured against: the live site's profile if live, else the latest."""
    live = pub.live_version(session, business_id)
    version = session.get(ProfileVersion, live.profile_version_id) if live else None
    profile = (version or _latest(session, business_id)).profile
    return [p.model_dump() for p in BusinessProfile.model_validate(profile).products]


def busy(session: Session, business_id: int) -> bool:
    """True while the agent or a customer test is working on the draft."""
    from app.services.agent import active_run

    if active_run(session, business_id) is not None:
        return True
    running = session.exec(
        select(Job.id).where(
            Job.business_id == business_id,
            Job.kind == SCORE_JOB,
            col(Job.status).in_(("queued", "running")),
        )
    ).first()
    return running is not None


def previous(session: Session, business_id: int) -> Previous | None:
    """The profile version immediately before the current one, if there is one."""
    latest = _latest(session, business_id)
    before = session.exec(
        select(ProfileVersion).where(
            ProfileVersion.business_id == business_id,
            ProfileVersion.version == latest.version - 1,
        )
    ).first()
    if before is None:
        return None
    products = BusinessProfile.model_validate(before.profile).products
    return Previous(before.version, before.created_at, len(products))


def save(
    engine: Engine,
    settings: Settings,
    business_id: int,
    products: list[dict[str, Any]],
    note: str = "menu and prices",
) -> bool:
    """Save the products as a new profile version, rebuild and re-test the draft.

    Returns False if nothing changed. Refused while the draft is being worked on.
    """
    with pub.business_lock(business_id), Session(engine) as session:
        if busy(session, business_id):
            raise AppError("menu.busy", status_code=409)
        latest = _latest(session, business_id)
        data = {**latest.profile, "products": products}
        try:
            profile = BusinessProfile.model_validate(data)
        except ValidationError as exc:
            raise AppError("menu.invalid", status_code=422) from exc
        if profile.model_dump() == BusinessProfile.model_validate(latest.profile).model_dump():
            return False
        session.add(
            ProfileVersion(
                business_id=business_id,
                version=latest.version + 1,
                profile=profile.model_dump(),
                created_by="owner",
                note=note,
            )
        )
        session.commit()
    pub.build_draft(engine, settings, business_id)
    queue_score(engine, business_id, "draft")
    return True


def restore_previous(engine: Engine, settings: Settings, business_id: int) -> bool:
    """Make the previous version's products current again, as a new version (twice swaps back)."""
    with Session(engine) as session:
        before = previous(session, business_id)
        if before is None:
            raise AppError("menu.nothing_to_restore", status_code=409)
        version = session.exec(
            select(ProfileVersion).where(
                ProfileVersion.business_id == business_id,
                ProfileVersion.version == before.version,
            )
        ).one()
        products = [
            p.model_dump() for p in BusinessProfile.model_validate(version.profile).products
        ]
    return save(engine, settings, business_id, products, note="restored the previous menu")
