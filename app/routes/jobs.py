"""Job status for the owner app to poll."""

from fastapi import APIRouter, HTTPException, Request

from app.db import get_engine
from app.services.i18n import pick_language, t
from app.services.jobs import get_job

router = APIRouter()


@router.get("/api/jobs/{job_id}")
def job_status(job_id: str, request: Request) -> dict[str, object]:
    """Status of one background job. Ids are unguessable; no private data is returned."""
    job = get_job(get_engine(), job_id)
    if job is None:
        raise HTTPException(status_code=404)
    lang = pick_language(request)
    message = (
        t(f"score.job.{job.status}", lang) if job.status in ("queued", "running", "failed") else ""
    )
    return {
        "status": job.status,
        "attempts": job.attempts,
        "run_id": job.run_id,
        "message": message,
    }
