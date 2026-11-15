"""Development-only pages: every sample business in every theme. Returns 404 in production."""

from datetime import date
from functools import lru_cache

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse, RedirectResponse
from sqlmodel import Session, col, select
from starlette.responses import Response

from app.config import get_settings
from app.db import get_engine
from app.models import Business, ScoreItem, TestRun
from app.services.jobs import enqueue, get_job
from app.services.samples import get_sample, load_samples
from app.services.score import PARTS
from app.services.score_runs import SCORE_JOB
from app.services.security import site_security_headers, verify_csrf
from app.services.sitegen import SiteBuild, SiteInput, build_site
from app.services.sites import main_origin, site_urls
from app.services.themes import THEMES
from app.templating import render

PREVIEW_DATE = date(2026, 1, 1)


def development_only() -> None:
    """Hide these routes in production."""
    if get_settings().is_production:
        raise HTTPException(status_code=404)


router = APIRouter(prefix="/dev", dependencies=[Depends(development_only)])


@lru_cache(maxsize=32)
def _preview_build(slug: str, theme: str) -> SiteBuild:
    sample = get_sample(slug)
    if sample is None or theme not in THEMES:
        raise HTTPException(status_code=404)
    settings = get_settings()
    urls = site_urls(slug, settings)
    return build_site(
        SiteInput(
            slug=slug,
            display_name=sample.display_name,
            profile=sample.profile,
            theme=theme,
            is_sample=True,
            prices_updated=PREVIEW_DATE,
            site_url=urls.site,
            main_url=urls.main,
            enquiry_url=urls.enquiry,
            app_name=settings.app_name,
            photos=sample.photos,
        )
    )


@router.get("/themes", include_in_schema=False)
def themes(request: Request) -> Response:
    """Every sample in all three themes, side by side."""
    return render(
        request, "app/dev_themes.html", {"samples": load_samples(), "themes": list(THEMES)}
    )


@router.get("/preview/{slug}/{theme}/{path:path}", include_in_schema=False)
def preview(slug: str, theme: str, path: str) -> Response:
    """Serve one file of a sample site built in memory."""
    build = _preview_build(slug, theme)
    key = path if path and not path.endswith("/") else f"{path}index.html"
    data = build.files.get(key)
    if data is None:
        raise HTTPException(status_code=404)
    settings = get_settings()
    headers = site_security_headers(settings, main_origin(settings), framable=True)
    return Response(data, media_type=_media_type(key), headers=headers)


def _media_type(path: str) -> str:
    for suffix, media in (
        (".html", "text/html; charset=utf-8"),
        (".css", "text/css"),
        (".woff2", "font/woff2"),
        (".svg", "image/svg+xml"),
        (".webp", "image/webp"),
    ):
        if path.endswith(suffix):
            return media
    return "application/octet-stream"


@router.get("/score", include_in_schema=False)
def score_index(request: Request) -> Response:
    """Businesses with a button to run the customer tests, and recent runs."""
    with Session(get_engine()) as session:
        businesses = session.exec(select(Business).where(col(Business.deleted_at).is_(None))).all()
        runs = session.exec(select(TestRun).order_by(col(TestRun.id).desc()).limit(20)).all()
    return render(request, "app/dev_score_index.html", {"businesses": businesses, "runs": runs})


@router.post("/score/{slug}", include_in_schema=False, dependencies=[Depends(verify_csrf)])
def start_score(slug: str, request: Request) -> RedirectResponse:
    """Queue a customer-test run for one business."""
    with Session(get_engine()) as session:
        business = session.exec(select(Business).where(Business.slug == slug)).first()
    if business is None:
        raise HTTPException(status_code=404)
    port = request.url.port or 8000
    job_id = enqueue(
        get_engine(), SCORE_JOB, business.id, {"url": f"http://{slug}.localhost:{port}/"}
    )
    return RedirectResponse(f"/dev/score/job/{job_id}", status_code=303)


@router.get("/score/job/{job_id}", include_in_schema=False)
def score_job(job_id: str, request: Request) -> Response:
    """Job status page; refreshes itself until the run is ready."""
    job = get_job(get_engine(), job_id)
    if job is None:
        raise HTTPException(status_code=404)
    if job.status == "succeeded" and job.run_id is not None:
        return RedirectResponse(f"/dev/score/run/{job.run_id}", status_code=303)
    return render(request, "app/dev_score_job.html", {"job": job})


@router.get("/score/run/{run_id}", include_in_schema=False)
def score_run(run_id: int, request: Request) -> Response:
    """The score report: total, five parts, every item with its reason and label."""
    with Session(get_engine()) as session:
        run = session.get(TestRun, run_id)
        if run is None:
            raise HTTPException(status_code=404)
        business = session.get(Business, run.business_id)
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
    return render(
        request, "app/dev_score_run.html", {"run": run, "business": business, "parts": parts}
    )


@router.get("/score/run/{run_id}/screenshot.png", include_in_schema=False)
def score_screenshot(run_id: int) -> FileResponse:
    """The phone screenshot taken during a run."""
    with Session(get_engine()) as session:
        run = session.get(TestRun, run_id)
    if run is None or not run.screenshot_path:
        raise HTTPException(status_code=404)
    path = (get_settings().runs_dir / run.screenshot_path).resolve()
    if not path.is_relative_to(get_settings().runs_dir.resolve()) or not path.is_file():
        raise HTTPException(status_code=404)
    return FileResponse(path, media_type="image/png")
