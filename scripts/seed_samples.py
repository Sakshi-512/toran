"""Create or update the sample businesses and build their sites.

Run: python -m scripts.seed_samples   (or make seed)
"""

import sys
from datetime import date

from sqlalchemy import func
from sqlmodel import Session, select

from app.config import get_settings
from app.db import get_engine
from app.models import Business, ProfileVersion, utcnow
from app.services.samples import Sample, load_samples
from app.services.sites import build_business_site


def upsert_sample(session: Session, sample: Sample) -> Business:
    """Create or update one sample business and save a new profile version if it changed."""
    business = session.exec(select(Business).where(Business.slug == sample.slug)).first()
    if business is None:
        business = Business(slug=sample.slug, display_name=sample.display_name)
    business.display_name = sample.display_name
    business.business_type = sample.profile.business_type
    business.theme = sample.theme
    business.is_sample = True
    business.source = "seed"
    business.status = "live"
    business.updated_at = utcnow()
    session.add(business)
    session.commit()
    session.refresh(business)
    profile = sample.profile.model_dump(mode="json")
    latest = session.exec(
        select(ProfileVersion)
        .where(ProfileVersion.business_id == business.id)
        .order_by(ProfileVersion.version.desc())  # type: ignore[attr-defined]
    ).first()
    if latest is None or latest.profile != profile:
        version = (latest.version if latest else 0) + 1
        assert business.id is not None  # noqa: S101 - set by commit above
        session.add(
            ProfileVersion(
                business_id=business.id,
                version=version,
                profile=profile,
                created_by="sample",
                note="sample data",
            )
        )
        session.commit()
    return business


def last_profile_date(session: Session, business_id: int) -> date:
    """Date of the latest profile version (the "prices last updated" date)."""
    latest = session.exec(
        select(func.max(ProfileVersion.created_at)).where(ProfileVersion.business_id == business_id)
    ).one()
    return latest.date() if latest else date.today()


def main() -> int:
    """Seed every sample and print a line per site."""
    settings = get_settings()
    with Session(get_engine()) as session:
        for sample in load_samples():
            business = upsert_sample(session, sample)
            assert business.id is not None  # noqa: S101
            build = build_business_site(
                business,
                sample.profile,
                settings,
                last_profile_date(session, business.id),
                sample.photos,
            )
            print(f"{business.slug}: {len(build.files)} files, {len(build.warnings)} warnings")
    return 0


if __name__ == "__main__":
    sys.exit(main())
