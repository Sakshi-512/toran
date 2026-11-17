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
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import delete, update
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, col, select

from app.config import Settings
from app.models import (
    ActionLog,
    AgentRun,
    Business,
    ClaudeUsage,
    DomainLookup,
    DomainRequest,
    Enquiry,
    IntakeSession,
    Job,
    OwnerDevice,
    OwnerQuestion,
    Photo,
    ProfileVersion,
    ScoreItem,
    SiteExtras,
    SiteVersion,
    SttUsage,
    TestRun,
    new_session_epoch,
    utcnow,
)
from app.schemas import BusinessProfile
from app.services.errors import AppError
from app.services.photos import list_photos, photo_dir, photo_path
from app.services.site_extras import approve_current, current_extras
from app.services.site_images import SitePhoto
from app.services.sitegen import SiteBuild, SiteInput, build_site, write_site
from app.services.sites import site_urls

logger = logging.getLogger(__name__)

SAMPLE_HOURS = 7 * 24  # judges can revisit what they made for a week


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
    """Build (in memory) the site from her latest profile and photos, plus the agent's current
    translations and layout (never facts)."""
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
            extras=current_extras(session, business.id),
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


def needs_phone(business: Business, profile: BusinessProfile | None) -> bool:
    """A real business needs a phone number (or her choice of no order button) to go live."""
    if business.is_sample or profile is None:
        return False
    return not profile.phone and bool(profile.order_methods)


_LOCKS: dict[int, threading.Lock] = {}
_LOCKS_GUARD = threading.Lock()


@contextmanager
def business_lock(business_id: int) -> Iterator[None]:
    """One publish, undo or offline change at a time per business (the app is one process;
    the unique version number is the database's own backstop)."""
    with _LOCKS_GUARD:
        lock = _LOCKS.setdefault(business_id, threading.Lock())
    with lock:
        yield


def publish(
    engine: Engine, settings: Settings, business_id: int, approved_hash: str | None = None
) -> SiteVersion:
    """Make the current draft the live site (after her approval) and save it as a version.

    Refused while the agent is changing the draft, and, when approved_hash is given (the draft
    she saw on the confirmation screen), if the draft has changed since (rule 2). The version
    folder is written first, then the database, and only then is the live folder replaced, so a
    failed save never changes what customers see.
    """
    with business_lock(business_id):
        return _publish(engine, settings, business_id, approved_hash)


def _publish(
    engine: Engine, settings: Settings, business_id: int, approved_hash: str | None
) -> SiteVersion:
    from app.services.agent import active_run
    from app.services.score_runs import site_hash

    with Session(engine) as session:
        business = _business(session, business_id)
        paths = folders(settings, business.slug)
        if not (paths.draft / "index.html").is_file():
            raise AppError("publish.no_draft", status_code=409)
        if active_run(session, business_id) is not None:
            raise AppError("publish.agent_busy", status_code=409)
        if approved_hash is not None and approved_hash != site_hash(paths.draft):
            raise AppError("publish.draft_changed", status_code=409)
        profile = latest_profile(session, business_id)
        confirmed = BusinessProfile.model_validate(profile.profile) if profile else None
        if needs_phone(business, confirmed):
            raise AppError("phone.required", status_code=409)
        numbers = session.exec(
            select(SiteVersion.number).where(SiteVersion.business_id == business_id)
        ).all()
        number = max(numbers, default=0) + 1
        version_dir = paths.version(number)
        shutil.rmtree(version_dir, ignore_errors=True)  # left by a publish that failed to save
        shutil.copytree(paths.draft, version_dir)
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
            site_hash=site_hash(version_dir),
            approved_at=now,
            published_at=now,
        )
        session.add(version)
        _approve_agent_changes(session, business_id, now)
        business.status = "live"
        if business.first_live_at is None:
            business.first_live_at = now
            _record_time_to_live(session, business_id, now)
        session.add(business)
        try:
            session.commit()
        except IntegrityError as exc:  # another process saved this version number first
            raise AppError("publish.try_again", status_code=409) from exc
        _copy(version_dir, paths.live)
        session.refresh(version)
        logger.info("site_published", extra={"business_id": business_id, "version": number})
        return version


def _approve_agent_changes(session: Session, business_id: int, now: datetime) -> None:
    """Publishing the draft is her approval of the agent's changes in it (rule 2)."""
    pending = session.exec(
        select(AgentRun).where(
            AgentRun.business_id == business_id,
            AgentRun.status == "done",
            col(AgentRun.approved_at).is_(None),
            col(AgentRun.discarded_at).is_(None),
        )
    ).all()
    for run in pending:
        run.approved_at = now
        session.add(run)
    approve_current(session, business_id)


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
    with business_lock(business_id):
        return _undo_publish(engine, settings, business_id)


def _undo_publish(engine: Engine, settings: Settings, business_id: int) -> SiteVersion | None:
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
    with business_lock(business_id), Session(engine) as session:
        business = _business(session, business_id)
        if live_version(session, business_id) is None:
            raise AppError("publish.not_published", status_code=409)
        business.status = "offline" if offline else "live"
        session.add(business)
        session.commit()


def delete_business(engine: Engine, settings: Settings, business_id: int) -> bool:
    """Her "delete everything": stop the site and every session now, then wipe all data.

    Step one happens at once: the business is marked deleted (no site, no link, no sessions,
    no enquiries) and its queued jobs are dropped. The wipe follows straight away, unless a
    job for it is still running; then the hourly cleanup wipes it once that job has ended
    (purge_deleted). The row keeps its web address until the wipe, so files a running job
    still writes can never pass to a new business. Returns True if everything is gone now.
    """
    with Session(engine) as session:
        business = session.get(Business, business_id)
        if business is None:
            return True
        if business.deleted_at is None:
            business.deleted_at = utcnow()
        business.status = "deleted"
        business.session_epoch = new_session_epoch()
        session.add(business)
        session.exec(
            delete(Job).where(col(Job.business_id) == business_id, col(Job.status) == "queued")
        )
        session.exec(delete(OwnerDevice).where(col(OwnerDevice.business_id) == business_id))
        session.commit()
        running = _running_jobs(session, business_id)
    logger.info("business_marked_deleted", extra={"business_id": business_id})
    if running:
        return False
    delete_business_data(engine, settings, business_id)
    return True


def _running_jobs(session: Session, business_id: int) -> int:
    return len(
        session.exec(
            select(Job.id).where(Job.business_id == business_id, Job.status == "running")
        ).all()
    )


def purge_deleted(engine: Engine, settings: Settings) -> int:
    """Wipe businesses marked deleted whose jobs have all ended. Returns how many."""
    with Session(engine) as session:
        marked = session.exec(
            select(Business.id).where(col(Business.deleted_at).is_not(None))
        ).all()
        ready = [b for b in marked if b is not None and not _running_jobs(session, b)]
    for business_id in ready:
        delete_business_data(engine, settings, business_id)
    return len(ready)


def delete_business_data(engine: Engine, settings: Settings, business_id: int) -> None:
    """Delete a business and all its data and files. Usage rows stay, without the link.

    Call delete_business instead: it first stops everything and waits for running jobs.
    """
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
            if intake.id is not None:
                shutil.rmtree(settings.uploads_dir / "intake" / str(intake.id), ignore_errors=True)
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
            delete(OwnerDevice).where(col(OwnerDevice.business_id) == business_id),
            delete(OwnerQuestion).where(col(OwnerQuestion.business_id) == business_id),
            delete(SiteExtras).where(col(SiteExtras.business_id) == business_id),
            delete(AgentRun).where(col(AgentRun.business_id) == business_id),
            delete(DomainLookup).where(col(DomainLookup.business_id) == business_id),
            delete(DomainRequest).where(col(DomainRequest.business_id) == business_id),
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
    """Delete sample businesses made from the sample voice notes after 7 days."""
    cutoff = (now or utcnow()) - timedelta(hours=hours)
    with Session(engine) as session:
        expired = session.exec(
            select(Business.id).where(
                col(Business.is_sample) == True,  # noqa: E712 - SQL expression
                col(Business.source) == "intake",
                col(Business.created_at) < cutoff,
                col(Business.deleted_at).is_(None),
            )
        ).all()
    for business_id in expired:
        if business_id is not None:
            delete_business(engine, settings, business_id)
    return len(expired)
