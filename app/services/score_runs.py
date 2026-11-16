"""Runs the customer tests for a business, scores the site and stores the run.

Every run is linked to the profile version and a hash of the exact site files tested, so scores
before and after fixes can be compared.
"""

import asyncio
import hashlib
import logging
import time
from dataclasses import dataclass
from datetime import UTC
from pathlib import Path
from typing import Any

from sqlalchemy.engine import Engine
from sqlmodel import Session, col, select

from app.config import Settings
from app.models import Business, IntakeSession, Job, ProfileVersion, ScoreItem, TestRun, utcnow
from app.schemas import BusinessProfile
from app.services.address_guard import looks_like_address
from app.services.ai_checks import AIResult, run_ai_checks
from app.services.customer_tests import Expectations, median_measurements, run_customer_tests
from app.services.jobs import Runner, enqueue
from app.services.score import Context, Score, score
from app.services.sites import internal_origin, site_dir, site_origin

logger = logging.getLogger(__name__)

SCORE_JOB = "score"


def site_hash(root: Path) -> str:
    """Hash of every file in a site (path and content), identifying the exact version tested."""
    digest = hashlib.sha256()
    for path in sorted(p for p in root.rglob("*") if p.is_file()):
        digest.update(path.relative_to(root).as_posix().encode())
        digest.update(hashlib.sha256(path.read_bytes()).digest())
    return digest.hexdigest()


def expectations(profile: BusinessProfile) -> Expectations:
    """What the site should show, from the confirmed profile (areas that are not addresses)."""
    areas = [a for a in [profile.area, *profile.delivery_areas] if a and not looks_like_address(a)]
    return Expectations(phone_digits=profile.phone.lstrip("+"), areas=tuple(areas))


def _latest_profile(session: Session, business_id: int) -> ProfileVersion | None:
    return session.exec(
        select(ProfileVersion)
        .where(ProfileVersion.business_id == business_id)
        .order_by(col(ProfileVersion.version).desc())
    ).first()


def save_run(
    engine: Engine,
    business: Business,
    job_id: str | None,
    version: ProfileVersion | None,
    url: str,
    result: Score,
    screenshot: bytes,
    duration: float,
    settings: Settings,
    ai: AIResult | None = None,
    target: "Target | None" = None,
) -> int:
    """Store a run and its items; write the phone screenshot to RUNS_DIR."""
    assert business.id is not None  # noqa: S101
    with Session(engine) as session:
        run = TestRun(
            business_id=business.id,
            job_id=job_id,
            profile_version_id=version.id if version else None,
            site_hash=site_hash(target.root if target else site_dir(business.slug, settings)),
            site_kind=target.kind if target else "live",
            site_version_id=target.version_id if target else None,
            url=url,
            total=result.total,
            possible_so_far=result.possible_so_far,
            pending_points=result.pending_points,
            duration_seconds=round(duration, 1),
            ai_status=ai.status if ai else None,
            ai_model=ai.model if ai else None,
            ai_cost_usd_estimate=ai.cost_usd if ai else 0.0,
        )
        session.add(run)
        session.commit()
        session.refresh(run)
        assert run.id is not None  # noqa: S101
        settings.runs_dir.mkdir(parents=True, exist_ok=True)
        shot = settings.runs_dir / f"run-{run.id}.png"
        shot.write_bytes(screenshot)
        run.screenshot_path = shot.name
        for item in result.items:
            session.add(
                ScoreItem(
                    run_id=run.id,
                    part=item.part,
                    key=item.key,
                    source=item.source,
                    status=item.status,
                    points=item.points,
                    max_points=item.max_points,
                    reason_en=item.reasons["en"],
                    reason_hi=item.reasons["hi"],
                    reason_mr=item.reasons["mr"],
                    detail=item.detail,
                )
            )
        session.add(run)
        session.commit()
        return run.id


@dataclass(frozen=True)
class Target:
    """What a score job tests: the draft (internal address) or the live site."""

    kind: str
    root: Path
    url: str
    version_id: int | None


def _target(
    session: Session, business: Business, payload: dict[str, Any], settings: Settings
) -> Target:
    from app.services.publish import folders, live_version

    kind = "draft" if payload.get("kind") == "draft" else "live"
    paths = folders(settings, business.slug)
    if kind == "draft":
        url = f"{internal_origin(settings)}/internal/draft/{business.slug}/"
        return Target("draft", paths.draft, str(payload.get("url") or url), None)
    assert business.id is not None  # noqa: S101
    live = live_version(session, business.id)
    url = f"{site_origin(business.slug, settings)}/"
    return Target("live", paths.live, str(payload.get("url") or url), live.id if live else None)


async def run_score_job(job_id: str, engine: Engine, settings: Settings) -> int:
    """Test a draft or live site three times, score the median and store the run."""
    with Session(engine) as session:
        job = session.get(Job, job_id)
        if job is None or job.business_id is None:
            raise LookupError("job or business missing")
        business = session.get(Business, job.business_id)
        version = _latest_profile(session, job.business_id)
        if business is None or version is None:
            raise LookupError("business has no profile yet")
        target = _target(session, business, job.payload or {}, settings)
        session.expunge(business)
        session.expunge(version)
    profile = BusinessProfile.model_validate(version.profile)
    started = time.monotonic()
    runs = await run_customer_tests(target.url, expectations(profile))
    measurements = median_measurements(runs)
    screenshot = runs[len(runs) // 2].screenshot
    ai = await asyncio.to_thread(
        run_ai_checks,
        engine,
        settings,
        business_id=business.id,
        slug=business.slug,
        business_type=profile.business_type,
        is_sample=business.is_sample,
        site_root=target.root,
        screenshot=screenshot,
    )
    live_https = settings.is_production and target.kind == "live"
    context = Context(is_sample=business.is_sample, is_production=live_https)
    result = score(measurements, context, ai)
    duration = time.monotonic() - started
    run_id = save_run(
        engine,
        business,
        job_id,
        version,
        target.url,
        result,
        screenshot,
        duration,
        settings,
        ai,
        target,
    )
    if target.kind == "draft":
        _record_time_to_draft_scored(engine, business)
    logger.info(
        "score_run_saved",
        extra={
            "business_id": business.id,
            "run_id": run_id,
            "kind": target.kind,
            "total": result.total,
            "seconds": round(duration, 1),
            "ai_status": ai.status,
            "ai_cost_usd": ai.cost_usd,
        },
    )
    return run_id


def _record_time_to_draft_scored(engine: Engine, business: Business) -> None:
    """System time from voice-note submission to the first draft score (excludes her decisions)."""
    with Session(engine) as session:
        intake = session.exec(
            select(IntakeSession).where(IntakeSession.business_id == business.id)
        ).first()
        if intake is None or intake.seconds_to_draft_scored is not None:
            return
        created = (
            intake.created_at if intake.created_at.tzinfo else intake.created_at.replace(tzinfo=UTC)
        )
        intake.seconds_to_draft_scored = int((utcnow() - created).total_seconds())
        session.add(intake)
        session.commit()


def queue_score(engine: Engine, business_id: int, kind: str, url: str | None = None) -> str:
    """Queue a score job for a business's draft or live site."""
    payload: dict[str, Any] = {"kind": kind}
    if url:
        payload["url"] = url
    return enqueue(engine, SCORE_JOB, business_id, payload)


def score_runner(engine: Engine, settings: Settings) -> Runner:
    """The job runner for score jobs."""

    async def runner(job_id: str) -> int:
        return await run_score_job(job_id, engine, settings)

    return runner


def report_parts(session: Session, run_id: int) -> tuple[TestRun, list[dict[str, Any]]]:
    """A run and its items grouped into the five parts, for the report pages."""
    from app.services.score import PARTS

    run = session.get(TestRun, run_id)
    if run is None:
        raise LookupError("run missing")
    items = session.exec(
        select(ScoreItem).where(ScoreItem.run_id == run_id).order_by(col(ScoreItem.id))
    ).all()
    parts = []
    for part in PARTS:
        part_items = [i for i in items if i.part == part]
        scored = [i for i in part_items if i.status == "scored"]
        parts.append(
            {
                "key": part,
                "checks": part_items,
                "points": sum(i.points for i in scored),
                "possible": sum(i.max_points for i in scored),
            }
        )
    return run, parts
