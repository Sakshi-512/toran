"""Business sites: URLs, building a business's site into SITES_DIR, and serving it by host name.

A site is served for <slug>.BASE_DOMAIN, and also for <slug>.localhost in development only.
"""

import logging
import mimetypes
import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from sqlmodel import Session, select
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import Headers
from starlette.requests import Request
from starlette.responses import FileResponse, Response
from starlette.types import ASGIApp, Receive, Scope, Send

from app.config import Settings, get_settings
from app.db import get_engine
from app.models import Business
from app.schemas import BusinessProfile
from app.services.errors import render_error
from app.services.security import site_security_headers
from app.services.site_images import SitePhoto
from app.services.sitegen import SiteBuild, SiteInput, build_site, write_site

logger = logging.getLogger(__name__)

DEV_PORT = 8000
LIVE_DIR = "live"
# Never a business: our own names, plus names mail clients, DNS and people expect to be ours
# (with the * DNS record, a business called "login" or "autodiscover" would be served there).
RESERVED_LABELS = frozenset(
    {
        "www",
        "api",
        "admin",
        "mail",
        "app",
        "autodiscover",
        "autoconfig",
        "webmail",
        "smtp",
        "imap",
        "pop",
        "email",
        "mx",
        "ftp",
        "ns1",
        "ns2",
        "login",
        "secure",
        "support",
        "help",
        "status",
    }
)
_LABEL = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
APP_STATIC_PREFIX = "/static/"
IMMUTABLE = "public, max-age=31536000, immutable"
REVALIDATE = "no-cache"


@dataclass(frozen=True)
class SiteUrls:
    """Absolute URLs baked into a site at build time."""

    site: str
    main: str
    enquiry: str


def subdomain_label(host: str, base_domain: str) -> str | None:
    """Return "shop" for "shop.<base_domain>", or None if host is not a direct subdomain."""
    host = host.strip().lower().rstrip(".")
    suffix = "." + base_domain
    if not host.endswith(suffix):
        return None
    label = host[: -len(suffix)]
    return label if _LABEL.match(label) else None


def main_origin(settings: Settings) -> str:
    """Origin of the main app."""
    if settings.is_production:
        return f"https://{settings.base_domain}"
    return f"http://localhost:{DEV_PORT}"


def internal_origin(settings: Settings) -> str:
    """How the app reaches itself (for testing drafts on /internal/draft/, never public)."""
    return f"http://127.0.0.1:{DEV_PORT}"


def site_origin(slug: str, settings: Settings) -> str:
    """Origin of a business site in the current environment."""
    if settings.is_production:
        return f"https://{slug}.{settings.base_domain}"
    return f"http://{slug}.localhost:{DEV_PORT}"


def allowed_enquiry_origins(slug: str, settings: Settings) -> frozenset[str]:
    """Exact origins allowed to post a business's enquiry form."""
    origins = {f"https://{slug}.{settings.base_domain}"}
    if not settings.is_production:
        origins.add(f"http://{slug}.localhost:{DEV_PORT}")
    return frozenset(origins)


def site_urls(slug: str, settings: Settings) -> SiteUrls:
    """URLs for a business site in the current environment."""
    main = main_origin(settings)
    return SiteUrls(site_origin(slug, settings), main, f"{main}/api/enquiry/{slug}")


def slug_from_host(host: str, settings: Settings) -> str | None:
    """The business slug a Host header points at, or None for the main app."""
    hostname = host.split(":", 1)[0]
    label = subdomain_label(hostname, settings.base_domain)
    if label is None and not settings.is_production:
        label = subdomain_label(hostname, "localhost")
    return None if label is None or label in RESERVED_LABELS else label


def active_business(session: Session, slug: str) -> Business | None:
    """The business with this slug, unless it has been deleted."""
    statement = select(Business).where(Business.slug == slug, Business.deleted_at.is_(None))  # type: ignore[union-attr]
    return session.exec(statement).first()


def site_dir(slug: str, settings: Settings) -> Path:
    """Folder holding a business's live site."""
    return settings.sites_dir / slug / LIVE_DIR


def build_business_site(
    business: Business,
    profile: BusinessProfile,
    settings: Settings,
    prices_updated: date,
    photos: tuple[SitePhoto, ...] = (),
) -> SiteBuild:
    """Build a business's site and write it to SITES_DIR. Returns the build (with warnings)."""
    urls = site_urls(business.slug, settings)
    build = build_site(
        SiteInput(
            slug=business.slug,
            display_name=business.display_name,
            profile=profile,
            theme=business.theme,
            is_sample=business.is_sample,
            prices_updated=prices_updated,
            site_url=urls.site,
            main_url=urls.main,
            enquiry_url=urls.enquiry,
            app_name=settings.app_name,
            photos=photos,
        )
    )
    write_site(build, site_dir(business.slug, settings))
    for warning in build.warnings:
        logger.info("site_warning", extra={"business_id": business.id, "code": warning.code})
    return build


def resolve_file(root: Path, url_path: str) -> Path | None:
    """Map a URL path to a file inside root, or None. Never leaves root."""
    relative = url_path.lstrip("/")
    candidate = (root / relative).resolve()
    root_resolved = root.resolve()
    if not candidate.is_relative_to(root_resolved):
        return None
    if candidate.is_dir():
        candidate = candidate / "index.html"
    return candidate if candidate.is_file() else None


def file_response(path: Path, settings: Settings, framable: bool = False) -> Response:
    """Serve one site file with site security headers and caching."""
    headers = site_security_headers(settings, main_origin(settings), framable=framable)
    headers["Cache-Control"] = IMMUTABLE if "/assets/" in path.as_posix() else REVALIDATE
    media_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    if path.suffix == ".woff2":
        media_type = "font/woff2"
    return FileResponse(path, media_type=media_type, headers=headers)


SERVED_STATUSES = frozenset({"live", "offline"})


def _lookup_file(slug: str, url_path: str, settings: Settings) -> tuple[str, Path | None]:
    """("live", file), ("offline", None) or ("missing", None). Drafts are never public."""
    with Session(get_engine()) as session:
        business = active_business(session, slug)
        status = business.status if business else "missing"
    if status == "offline":
        return "offline", None
    if status != "live":
        return "missing", None
    return "live", resolve_file(site_dir(slug, settings), url_path)


class SiteHostMiddleware:
    """Serves business sites for their subdomains; everything else goes to the main app."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Route by Host header."""
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        settings = get_settings()
        slug = slug_from_host(Headers(scope=scope).get("host", ""), settings)
        # The app stylesheet is also served on subdomains so friendly error pages are styled.
        if slug is None or scope["path"].startswith(APP_STATIC_PREFIX):
            await self.app(scope, receive, send)
            return
        request = Request(scope, receive)
        response = await self._site_response(request, slug, settings)
        await response(scope, receive, send)

    @staticmethod
    async def _site_response(request: Request, slug: str, settings: Settings) -> Response:
        if request.method not in ("GET", "HEAD"):
            return render_error(request, "error.not_found", 405)
        state, path = await run_in_threadpool(_lookup_file, slug, request.url.path, settings)
        if state == "offline":
            return render_error(request, "site.offline", 503)
        if path is None:
            return render_error(request, "error.not_found", 404)
        return file_response(path, settings)
