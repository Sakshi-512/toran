"""Owner site pages: preview, styles, scoring, publish (with approval), undo and offline."""

import re
from collections.abc import Sequence
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import FileResponse, RedirectResponse
from sqlmodel import Session, col, select
from starlette.responses import Response

from app.config import get_settings
from app.db import get_engine
from app.models import Business, Job, ScoreItem, TestRun
from app.schemas import BusinessProfile
from app.services import publish as pub
from app.services.agent_report import pending_changes
from app.services.auth import require_owner
from app.services.errors import AppError
from app.services.i18n import pick_language, queue_text, t
from app.services.jobs import queue_position
from app.services.score_runs import SCORE_JOB, queue_score, report_parts, site_hash
from app.services.score_summary import FIX_LINKS, OwnerSummary, owner_summary
from app.services.security import site_security_headers, verify_csrf
from app.services.sitegen import SiteBuild
from app.services.sites import main_origin, resolve_file, site_origin
from app.services.themes import THEMES
from app.templating import render

router = APIRouter(prefix="/owner")
_FORM = re.compile(r"<!--enquiry-form-->.*?<!--/enquiry-form-->", re.S)
_PREVIEW_CACHE: dict[tuple[Any, ...], SiteBuild] = {}
_CACHE_SIZE = 12
NOINDEX = {"X-Robots-Tag": "noindex, nofollow"}


def scoring_status(session: Session, business_id: int, lang: str) -> dict[str, Any] | None:
    """Progress of the newest score job: queued (with place in line), running or failed."""
    job = session.exec(
        select(Job)
        .where(Job.business_id == business_id, Job.kind == SCORE_JOB)
        .order_by(col(Job.created_at).desc())
    ).first()
    if job is None or job.status == "succeeded":
        return None
    if job.status == "failed":
        return {"state": "failed"}
    place, wait = queue_position(get_engine(), job.id)
    if job.status == "queued" and place > 1:
        return {"state": "queued", "text": queue_text(place, wait, lang)}
    return {"state": "running", "text": t("owner.scoring", lang)}


def _business(session: Session, business_id: int) -> Business:
    business = session.get(Business, business_id)
    if business is None or business.deleted_at is not None:
        raise AppError("error.not_found", status_code=404)
    return business


def owner_site_context(session: Session, business: Business, lang: str) -> dict[str, Any]:
    """Status, site actions, scoring progress and score history for the owner page."""
    assert business.id is not None  # noqa: S101
    settings = get_settings()
    runs = session.exec(
        select(TestRun).where(TestRun.business_id == business.id).order_by(col(TestRun.id).desc())
    ).all()
    paths = pub.folders(settings, business.slug)
    version = pub.latest_profile(session, business.id)
    profile = BusinessProfile.model_validate(version.profile) if version else None
    return {
        "needs_phone": pub.needs_phone(business, profile),
        "site_url": f"{site_origin(business.slug, settings)}/",
        "has_draft": (paths.draft / "index.html").is_file(),
        "live": pub.live_version(session, business.id),
        "runs": runs,
        "summaries": _summaries(session, runs),
        "latest_draft_run": next((r for r in runs if r.site_kind == "draft"), None),
        "scoring": scoring_status(session, business.id, lang),
    }


def _summaries(session: Session, runs: Sequence[TestRun]) -> dict[int, OwnerSummary]:
    ids = [r.id for r in runs if r.id is not None]
    items = session.exec(select(ScoreItem).where(col(ScoreItem.run_id).in_(ids))).all()
    by_run: dict[int, list[ScoreItem]] = {}
    for item in items:
        by_run.setdefault(item.run_id, []).append(item)
    return {r.id: owner_summary(r, by_run.get(r.id, [])) for r in runs if r.id is not None}


def _rebuild_and_score(business_id: int, theme: str | None = None) -> None:
    pub.build_draft(get_engine(), get_settings(), business_id, theme)
    queue_score(get_engine(), business_id, "draft")


@router.post("/draft", include_in_schema=False, dependencies=[Depends(verify_csrf)])
def rebuild(business_id: int = Depends(require_owner)) -> RedirectResponse:
    """Rebuild the draft from her latest details and photos, then test it again."""
    _rebuild_and_score(business_id)
    return RedirectResponse("/owner#site", status_code=303)


@router.get("/styles", include_in_schema=False)
def styles(request: Request, business_id: int = Depends(require_owner)) -> Response:
    """The three styles, previewed with her own details."""
    with Session(get_engine()) as session:
        business = _business(session, business_id)
        return render(
            request, "app/owner_styles.html", {"business": business, "themes": list(THEMES)}
        )


@router.post("/styles", include_in_schema=False, dependencies=[Depends(verify_csrf)])
async def choose_style(
    request: Request, business_id: int = Depends(require_owner)
) -> RedirectResponse:
    """Use a style: rebuild the draft and test it again."""
    theme = str((await request.form()).get("theme", ""))
    if theme not in THEMES:
        raise AppError("error.invalid_input", status_code=422)
    _rebuild_and_score(business_id, theme)
    return RedirectResponse("/owner#site", status_code=303)


def _preview_build(session: Session, business: Business, theme: str) -> SiteBuild:
    from app.services.photos import list_photos

    photos = tuple((p.id, p.is_main, p.subject) for p in list_photos(session, business.id or 0))
    version = pub.latest_profile(session, business.id or 0)
    key = (business.id, theme, version.id if version else None, photos, business.slug)
    if key not in _PREVIEW_CACHE:
        if len(_PREVIEW_CACHE) >= _CACHE_SIZE:
            _PREVIEW_CACHE.pop(next(iter(_PREVIEW_CACHE)))
        _PREVIEW_CACHE[key] = pub.build_for(session, get_settings(), business, theme)
    return _PREVIEW_CACHE[key]


def _preview_bytes(data: bytes, path: str, lang: str) -> bytes:
    if not path.endswith(".html"):
        return data
    note = f'<p class="notice">{t("site.preview_note", lang)}</p>'
    return _FORM.sub(note, data.decode("utf-8")).encode("utf-8")


def _media_type(path: str) -> str:
    for suffix, media in (
        (".html", "text/html; charset=utf-8"),
        (".css", "text/css"),
        (".woff2", "font/woff2"),
        (".svg", "image/svg+xml"),
        (".webp", "image/webp"),
        (".jpg", "image/jpeg"),
    ):
        if path.endswith(suffix):
            return media
    return "application/octet-stream"


@router.get("/style-preview/{theme}/{path:path}", include_in_schema=False)
def style_preview(
    theme: str, path: str, request: Request, business_id: int = Depends(require_owner)
) -> Response:
    """Her site in one style, built in memory (shown in a frame on the styles page)."""
    if theme not in THEMES:
        raise AppError("error.not_found", status_code=404)
    with Session(get_engine()) as session:
        build = _preview_build(session, _business(session, business_id), theme)
    key = path if path and not path.endswith("/") else f"{path}index.html"
    data = build.files.get(key)
    if data is None:
        raise AppError("error.not_found", status_code=404)
    settings = get_settings()
    headers = {**site_security_headers(settings, main_origin(settings), framable=True), **NOINDEX}
    return Response(
        _preview_bytes(data, key, pick_language(request)),
        media_type=_media_type(key),
        headers=headers,
    )


@router.get("/preview/{path:path}", include_in_schema=False)
def preview(path: str, request: Request, business_id: int = Depends(require_owner)) -> Response:
    """Her private draft preview: only she can open it, and search engines never index it."""
    settings = get_settings()
    with Session(get_engine()) as session:
        business = _business(session, business_id)
    file = resolve_file(pub.folders(settings, business.slug).draft, path)
    if file is None:
        raise AppError("error.not_found", status_code=404)
    headers = {
        **site_security_headers(settings, main_origin(settings)),
        **NOINDEX,
        "Cache-Control": "private, no-store",
    }
    data = _preview_bytes(file.read_bytes(), file.name, pick_language(request))
    return Response(data, media_type=_media_type(file.name), headers=headers)


@router.get("/score/{run_id}", include_in_schema=False)
def report(run_id: int, request: Request, business_id: int = Depends(require_owner)) -> Response:
    """A Readiness Score report for one of her runs, in her language."""
    with Session(get_engine()) as session:
        run, parts = _own_run(session, run_id, business_id)
        business = _business(session, business_id)
    items = [item for part in parts for item in part["checks"]]
    summary = owner_summary(run, items)
    return render(
        request,
        "app/owner_report.html",
        {"run": run, "summary": summary, "business": business, "fix_links": FIX_LINKS},
    )


def _own_run(
    session: Session, run_id: int, business_id: int
) -> tuple[TestRun, list[dict[str, Any]]]:
    try:
        run, parts = report_parts(session, run_id)
    except LookupError as exc:
        raise AppError("error.not_found", status_code=404) from exc
    if run.business_id != business_id:
        raise AppError("error.not_found", status_code=404)
    return run, parts


@router.get("/score/{run_id}/screenshot.png", include_in_schema=False)
def report_screenshot(run_id: int, business_id: int = Depends(require_owner)) -> FileResponse:
    """The phone screenshot of one of her runs."""
    settings = get_settings()
    with Session(get_engine()) as session:
        run, _ = _own_run(session, run_id, business_id)
    path = (settings.runs_dir / str(run.screenshot_path)).resolve()
    if (
        not run.screenshot_path
        or not path.is_relative_to(settings.runs_dir.resolve())
        or not path.is_file()
    ):
        raise AppError("error.not_found", status_code=404)
    return FileResponse(
        path, media_type="image/png", headers={"Cache-Control": "private, no-store"}
    )


@router.get("/publish", include_in_schema=False)
def publish_page(request: Request, business_id: int = Depends(require_owner)) -> Response:
    """The confirmation screen: nothing goes live without her saying yes here (rule 2)."""
    with Session(get_engine()) as session:
        business = _business(session, business_id)
        lang = pick_language(request)
        context = owner_site_context(session, business, lang)
        context["agent_changes"] = pending_changes(session, business_id, lang)
    if not context["has_draft"]:
        raise AppError("publish.no_draft", status_code=409)
    # The exact draft she is approving; publishing refuses if it changes before she says yes.
    context["draft_hash"] = site_hash(pub.folders(get_settings(), business.slug).draft)
    return render(request, "app/owner_publish.html", {"business": business, **context})


@router.post("/publish", include_in_schema=False, dependencies=[Depends(verify_csrf)])
async def publish_confirmed(
    request: Request, business_id: int = Depends(require_owner)
) -> RedirectResponse:
    """She said yes to the draft she saw: publish it and test the live site."""
    approved = str((await request.form()).get("draft_hash", ""))
    pub.publish(get_engine(), get_settings(), business_id, approved)
    queue_score(get_engine(), business_id, "live")
    return RedirectResponse("/owner#site", status_code=303)


@router.post("/undo", include_in_schema=False, dependencies=[Depends(verify_csrf)])
def undo(business_id: int = Depends(require_owner)) -> RedirectResponse:
    """Undo the last publish."""
    pub.undo_publish(get_engine(), get_settings(), business_id)
    return RedirectResponse("/owner#site", status_code=303)


@router.post("/offline", include_in_schema=False, dependencies=[Depends(verify_csrf)])
def offline(business_id: int = Depends(require_owner)) -> RedirectResponse:
    """Take the site offline."""
    pub.set_offline(get_engine(), business_id, offline=True)
    return RedirectResponse("/owner#site", status_code=303)


@router.post("/online", include_in_schema=False, dependencies=[Depends(verify_csrf)])
def online(business_id: int = Depends(require_owner)) -> RedirectResponse:
    """Bring the site back online."""
    pub.set_offline(get_engine(), business_id, offline=False)
    return RedirectResponse("/owner#site", status_code=303)


@router.post("/gallery", include_in_schema=False, dependencies=[Depends(verify_csrf)])
async def gallery_choice(request: Request, business_id: int = Depends(require_owner)) -> Response:
    """Her separate yes (or no) to showing her live site in the public gallery."""
    form = await request.form()
    with Session(get_engine()) as session:
        business = _business(session, business_id)
        if business.is_sample:
            raise AppError("error.not_found", status_code=404)
        business.gallery_opt_in = form.get("show") == "yes"
        session.add(business)
        session.commit()
    return RedirectResponse("/owner#gallery", status_code=303)
