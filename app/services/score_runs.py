"""Runs the customer tests for a business, scores the site and stores the run.

Every run is linked to the profile version and a hash of the exact site files tested, so scores
before and after fixes can be compared.
"""

import hashlib
import logging
import time
from pathlib import Path

from sqlalchemy.engine import Engine
from sqlmodel import Session, col, select

from app.config import Settings
from app.models import Business, Job, ProfileVersion, ScoreItem, TestRun
from app.schemas import BusinessProfile
from app.services.address_guard import looks_like_address
from app.services.customer_tests import Expectations, median_measurements, run_customer_tests
from app.services.jobs import Runner
from app.services.score import Context, Score, score
from app.services.sites import site_dir, site_origin

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
) -> int:
    """Store a run and its items; write the phone screenshot to RUNS_DIR."""
    assert business.id is not None  # noqa: S101
    with Session(engine) as session:
        run = TestRun(
            business_id=business.id,
            job_id=job_id,
            profile_version_id=version.id if version else None,
            site_hash=site_hash(site_dir(business.slug, settings)),
            url=url,
            total=result.total,
            possible_so_far=result.possible_so_far,
            pending_points=result.pending_points,
            duration_seconds=round(duration, 1),
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


async def run_score_job(job_id: str, engine: Engine, settings: Settings) -> int:
    """Test a business site three times, score the median and store the run."""
    with Session(engine) as session:
        job = session.get(Job, job_id)
        if job is None or job.business_id is None:
            raise LookupError("job or business missing")
        business = session.get(Business, job.business_id)
        version = _latest_profile(session, job.business_id)
        if business is None or version is None:
            raise LookupError("business has no profile yet")
        session.expunge(business)
        session.expunge(version)
        url = str((job.payload or {}).get("url") or f"{site_origin(business.slug, settings)}/")
    profile = BusinessProfile.model_validate(version.profile)
    started = time.monotonic()
    runs = await run_customer_tests(url, expectations(profile))
    measurements = median_measurements(runs)
    context = Context(is_sample=business.is_sample, is_production=settings.is_production)
    result = score(measurements, context)
    duration = time.monotonic() - started
    screenshot = runs[len(runs) // 2].screenshot
    run_id = save_run(
        engine, business, job_id, version, url, result, screenshot, duration, settings
    )
    logger.info(
        "score_run_saved",
        extra={
            "business_id": business.id,
            "run_id": run_id,
            "total": result.total,
            "seconds": round(duration, 1),
        },
    )
    return run_id


def score_runner(engine: Engine, settings: Settings) -> Runner:
    """The job runner for score jobs."""

    async def runner(job_id: str) -> int:
        return await run_score_job(job_id, engine, settings)

    return runner
