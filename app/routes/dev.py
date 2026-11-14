"""Development-only pages: every sample business in every theme. Returns 404 in production."""

from datetime import date
from functools import lru_cache

from fastapi import APIRouter, Depends, HTTPException, Request
from starlette.responses import Response

from app.config import get_settings
from app.services.samples import get_sample, load_samples
from app.services.security import site_security_headers
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
