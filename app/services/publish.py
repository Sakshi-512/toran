"""Draft and live versions of business sites, publishing with the owner's approval, and undo.

Folders under SITES_DIR/<slug>/:
- draft/ the site built from her latest confirmed profile, photos and theme (never public)
- live/ what customers see at <slug>.BASE_DOMAIN while the business is live
- versions/<n>/ every published version, kept for undo

Nothing is published without her explicit approval (rule 2): publish() is only called from the
owner's confirmation screen.
"""

import logging
import shutil
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import delete, update
from sqlalchemy.engine import Engine
from sqlmodel import Session, col, select

from app.config import Settings
from app.models import (
    ActionLog,
    Business,
    ClaudeUsage,
    Enquiry,
    IntakeSession,
    Job,
    Photo,
    ProfileVersion,
    ScoreItem,
    SiteVersion,
    SttUsage,
    TestRun,
    utcnow,
)
from app.schemas import BusinessProfile
from app.services.errors import AppError
from app.services.photos import list_photos, photo_dir, photo_path
from app.services.site_images import SitePhoto
from app.services.sitegen import SiteBuild, SiteInput, build_site, write_site
from app.services.sites import site_urls

logger = logging.getLogger(__name__)

SAMPLE_HOURS = 48


@dataclass(frozen=True)
class Folders:
    """Where one business's site files live."""

    root: Path

    @property
    def draft(self) -> Path:
        """The draft site."""
        return self.root / "draft"

    @property
    def live(self) -> Path:
        """The live site."""
        return self.root / "live"

    def version(self, number: int) -> Path:
        """A published version."""
        return self.root / "versions" / str(number)


def folders(settings: Settings, slug: str) -> Folders:
    """Site folders for a business."""
    return Folders(settings.sites_dir / slug)


def latest_profile(session: Session, business_id: int) -> ProfileVersion | None:
    """The newest confirmed profile version."""
    return session.exec(
        select(ProfileVersion)
        .where(ProfileVersion.business_id == business_id)
        .order_by(col(ProfileVersion.version).desc())
    ).first()


def site_photos(session: Session, settings: Settings, business_id: int) -> tuple[SitePhoto, ...]:
    """Her photos for the site: the main photo as the hero, product photos in their cards."""
    photos = []
    for photo in list_photos(session, business_id):
        path = photo_path(settings, photo)
        if not path.is_file():
            continue
        subject = None if photo.is_main else photo.subject
        if subject is None and not photo.is_main:
            continue  # a second photo of the business itself has no place yet
        photos.append(SitePhoto(subject=subject, data=path.read_bytes()))
    return tuple(photos)


def build_for(
    session: Session, settings: Settings, business: Business, theme: str | None = None
) -> SiteBuild:
    """Build (in memory) the site for a business from its latest profile and photos."""
    assert business.id is not None  # noqa: S101
    version = latest_profile(session, business.id)
    if version is None:
        raise AppError("error.not_found", status_code=404)
    urls = site_urls(business.slug, settings)
    return build_site(
        SiteInput(
            slug=business.slug,
            display_name=business.display_name,
            profile=BusinessProfile.model_validate(version.profile),
            theme=theme or business.theme,
            is_sample=business.is_sample,
            prices_updated=version.created_at.date(),
            site_url=urls.site,
            main_url=urls.main,
            enquiry_url=urls.enquiry,
            app_name=settings.app_name,
            photos=site_photos(session, settings, business.id),
        )
    )


def build_draft(
    engine: Engine, settings: Settings, business_id: int, theme: str | None = None
) -> SiteBuild:
    """Rebuild the draft (optionally with a new theme) and write it to draft/."""
    with Session(engine) as session:
        business = _business(session, business_id)
        if theme:
            business.theme = theme
            session.add(business)
            session.commit()
        build = build_for(session, settings, business)
        write_site(build, folders(settings, business.slug).draft)
    logger.info("draft_built", extra={"business_id": business_id, "warnings": len(build.warnings)})
    return build


def _business(session: Session, business_id: int) -> Business:
    business = session.get(Business, business_id)
    if business is None or business.deleted_at is not None:
        raise AppError("error.not_found", status_code=404)
    return business


def live_version(session: Session, business_id: int) -> SiteVersion | None:
    """The version customers currently see (if any)."""
    return session.exec(
        select(SiteVersion).where(
            SiteVersion.business_id == business_id, SiteVersion.status == "live"
        )
    ).first()


def _copy(source: Path, target: Path) -> None:
    """Replace target with a copy of source in one step."""
    staging = target.with_name(f".{target.name}-new")
    shutil.rmtree(staging, ignore_errors=True)
    shutil.copytree(source, staging)
    old = target.with_name(f".{target.name}-old")
    if target.exists():
        shutil.rmtree(old, ignore_errors=True)
        target.replace(old)
    staging.replace(target)
    shutil.rmtree(old, ignore_errors=True)


def publish(engine: Engine, settings: Settings, business_id: int) -> SiteVersion:
    """Make the current draft the live site (after her approval) and save it as a version."""
    from app.services.score_runs import site_hash

    with Session(engine) as session:
        business = _business(session, business_id)
        paths = folders(settings, business.slug)
        if not (paths.draft / "index.html").is_file():
            raise AppError("publish.no_draft", status_code=409)
        profile = latest_profile(session, business_id)
        numbers = session.exec(
            select(SiteVersion.number).where(SiteVersion.business_id == business_id)
        ).all()
        number = max(numbers, default=0) + 1
        shutil.copytree(paths.draft, paths.version(number))
        _copy(paths.version(number), paths.live)
        previous = live_version(session, business_id)
        if previous is not None:
            previous.status = "superseded"
            session.add(previous)
        now = utcnow()
        version = SiteVersion(
            business_id=business_id,
            number=number,
            theme=business.theme,
            profile_version_id=profile.id if profile else None,
            site_hash=site_hash(paths.live),
            approved_at=now,
            published_at=now,
        )
        session.add(version)
        business.status = "live"
        if business.first_live_at is None:
            business.first_live_at = now
            _record_time_to_live(session, business_id, now)
        session.add(business)
        session.commit()
        session.refresh(version)
        logger.info("site_published", extra={"business_id": business_id, "version": number})
        return version


def _record_time_to_live(session: Session, business_id: int, now: datetime) -> None:
    intake = session.exec(
        select(IntakeSession).where(IntakeSession.business_id == business_id)
    ).first()
    if intake is not None and intake.seconds_to_live is None:
        started = _aware(intake.created_at)
        intake.seconds_to_live = int((now - started).total_seconds())
        session.add(intake)


def _aware(moment: datetime) -> datetime:
    return moment.replace(tzinfo=UTC) if moment.tzinfo is None else moment


def undo_publish(engine: Engine, settings: Settings, business_id: int) -> SiteVersion | None:
    """Restore the previous published version. Undoing the first publish returns to draft."""
    with Session(engine) as session:
        business = _business(session, business_id)
        current = live_version(session, business_id)
        if current is None:
            raise AppError("publish.nothing_to_undo", status_code=409)
        current.status = "undone"
        session.add(current)
        previous = session.exec(
            select(SiteVersion)
            .where(
                SiteVersion.business_id == business_id,
                SiteVersion.status == "superseded",
                col(SiteVersion.number) < current.number,
            )
            .order_by(col(SiteVersion.number).desc())
        ).first()
        paths = folders(settings, business.slug)
        if previous is None:
            shutil.rmtree(paths.live, ignore_errors=True)
            business.status = "draft"
        else:
            _copy(paths.version(previous.number), paths.live)
            previous.status = "live"
            session.add(previous)
            business.status = "live"
        session.add(business)
        session.commit()
        logger.info(
            "site_publish_undone",
            extra={"business_id": business_id, "now_live": previous.number if previous else None},
        )
        return previous


def set_offline(engine: Engine, business_id: int, offline: bool) -> None:
    """Take the live site offline, or bring it back online."""
    with Session(engine) as session:
        business = _business(session, business_id)
        if live_version(session, business_id) is None:
            raise AppError("publish.not_published", status_code=409)
        business.status = "offline" if offline else "live"
        session.add(business)
        session.commit()


def delete_business_data(engine: Engine, settings: Settings, business_id: int) -> None:
    """Delete a business and all its data and files. Usage rows stay, without the link."""
    with Session(engine) as session:
        business = session.get(Business, business_id)
        if business is None:
            return
        runs = session.exec(select(TestRun).where(TestRun.business_id == business_id)).all()
        for run in runs:
            if run.screenshot_path:
                (settings.runs_dir / run.screenshot_path).unlink(missing_ok=True)
        run_ids = [r.id for r in runs]
        intakes = session.exec(
            select(IntakeSession).where(IntakeSession.business_id == business_id)
        ).all()
        intake_ids = [i.id for i in intakes]
        for intake in intakes:
            if intake.input_path:
                Path(intake.input_path).unlink(missing_ok=True)
        for table in (ClaudeUsage, SttUsage):
            session.exec(
                update(table)
                .where(
                    (col(table.business_id) == business_id) | col(table.intake_id).in_(intake_ids)
                )
                .values(business_id=None, intake_id=None)
            )
        statements = [
            delete(ScoreItem).where(col(ScoreItem.run_id).in_(run_ids)),
            delete(TestRun).where(col(TestRun.business_id) == business_id),
            delete(SiteVersion).where(col(SiteVersion.business_id) == business_id),
            delete(Photo).where(col(Photo.business_id) == business_id),
            delete(IntakeSession).where(col(IntakeSession.business_id) == business_id),
            delete(Enquiry).where(col(Enquiry.business_id) == business_id),
            delete(Job).where(col(Job.business_id) == business_id),
            delete(ActionLog).where(col(ActionLog.business_id) == business_id),
            delete(ProfileVersion).where(col(ProfileVersion.business_id) == business_id),
        ]
        for statement in statements:
            session.exec(statement)
        shutil.rmtree(folders(settings, business.slug).root, ignore_errors=True)
        shutil.rmtree(photo_dir(settings, business_id).parent, ignore_errors=True)
        session.delete(business)
        session.commit()
    logger.info("business_deleted", extra={"business_id": business_id})


def purge_expired_samples(
    engine: Engine, settings: Settings, now: datetime | None = None, hours: int = SAMPLE_HOURS
) -> int:
    """Delete sample businesses made from the sample voice notes after 48 hours."""
    cutoff = (now or utcnow()) - timedelta(hours=hours)
    with Session(engine) as session:
        expired = session.exec(
            select(Business.id).where(
                col(Business.is_sample) == True,  # noqa: E712 - SQL expression
                col(Business.source) == "intake",
                col(Business.created_at) < cutoff,
            )
        ).all()
    for business_id in expired:
        if business_id is not None:
            delete_business_data(engine, settings, business_id)
    return len(expired)
