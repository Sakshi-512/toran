"""Run the customer tests and score one business: python -m scripts.score_site <slug> [url]."""

import asyncio
import sys

from sqlmodel import Session, select

from app.config import get_settings
from app.db import get_engine
from app.models import Business
from app.services.jobs import JobWorker, enqueue, get_job
from app.services.score_runs import SCORE_JOB, score_runner
from app.services.sites import site_origin


def main(argv: list[str]) -> int:
    """Queue a score job, run it here, and print the result."""
    if not argv:
        print("usage: python -m scripts.score_site <slug> [url]")
        return 2
    slug = argv[0]
    settings, engine = get_settings(), get_engine()
    with Session(engine) as session:
        business = session.exec(select(Business).where(Business.slug == slug)).first()
    if business is None:
        print(f"No business with slug {slug}")
        return 1
    url = argv[1] if len(argv) > 1 else f"{site_origin(slug, settings)}/"
    job_id = enqueue(engine, SCORE_JOB, business.id, {"url": url})
    asyncio.run(JobWorker(engine, {SCORE_JOB: score_runner(engine, settings)}).run_until_idle())
    job = get_job(engine, job_id)
    if job is None or job.status != "succeeded":
        print(f"Job failed: {job.error if job else 'missing'}")
        return 1
    print(f"Run {job.run_id} saved for {slug} ({url})")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
