"""Health check and internal endpoints.

/internal/* is blocked by Caddy for public traffic. As a second guard, these routes refuse any
request that came through the reverse proxy (it always adds X-Forwarded-For). Caddy's on-demand
TLS "ask" call goes straight to the app container without that header.
"""

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import text
from sqlmodel import Session, select
from starlette.responses import Response

from app.config import get_settings
from app.db import get_session
from app.services.godaddy import gddy_status
from app.services.sites import (
    SERVED_STATUSES,
    active_business,
    file_response,
    resolve_file,
    subdomain_label,
)

router = APIRouter()


@router.get("/health", include_in_schema=False)
def health(request: Request, session: Session = Depends(get_session)) -> dict[str, str]:
    """Liveness and database check for Docker and Caddy health checks.

    Also reports the GoDaddy CLI ("installed", "missing" or "mock"). A missing CLI only hides
    web address suggestions, so the app still counts as healthy.
    """
    session.exec(select(text("1"))).one()
    return {"status": "ok", "gddy": gddy_status(get_settings())}


def require_direct_internal_call(request: Request) -> None:
    """Reject internal calls that came through the public reverse proxy."""
    if "x-forwarded-for" in request.headers or "forwarded" in request.headers:
        raise HTTPException(status_code=404)


@router.get(
    "/internal/tls-check",
    include_in_schema=False,
    dependencies=[Depends(require_direct_internal_call)],
)
def tls_check(
    request: Request, domain: str = "", session: Session = Depends(get_session)
) -> dict[str, str]:
    """Allow a certificate only for the main domain, www, or a live or offline business."""
    base = get_settings().base_domain
    host = domain.strip().lower().rstrip(".")
    if host in (base, "www." + base):
        return {"allowed": host}
    label = subdomain_label(host, base)
    business = active_business(session, label) if label and label != "www" else None
    if business is not None and business.status in SERVED_STATUSES:
        return {"allowed": host}
    raise HTTPException(status_code=404)


@router.get(
    "/internal/draft/{slug}/{path:path}",
    include_in_schema=False,
    dependencies=[Depends(require_direct_internal_call)],
)
def internal_draft(slug: str, path: str) -> Response:
    """A business's draft, for the app's own customer tests. Never reachable through Caddy."""
    from app.services.publish import folders

    file = resolve_file(folders(get_settings(), slug).draft, path)
    if file is None:
        raise HTTPException(status_code=404)
    return file_response(file, get_settings())
