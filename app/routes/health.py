"""Health check and internal endpoints.

/internal/* is blocked by Caddy for public traffic. As a second guard, these routes refuse any
request that came through the reverse proxy (it always adds X-Forwarded-For). Caddy's on-demand
TLS "ask" call goes straight to the app container without that header.
"""

import re

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import text
from sqlmodel import Session, select

from app.config import get_settings
from app.db import get_session
from app.models import Business

router = APIRouter()

_SUBDOMAIN_LABEL = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")


@router.get("/health", include_in_schema=False)
def health(request: Request, session: Session = Depends(get_session)) -> dict[str, str]:
    """Liveness and database check for Docker and Caddy health checks."""
    session.exec(select(text("1"))).one()
    return {"status": "ok"}


def require_direct_internal_call(request: Request) -> None:
    """Reject internal calls that came through the public reverse proxy."""
    if "x-forwarded-for" in request.headers or "forwarded" in request.headers:
        raise HTTPException(status_code=404)


def subdomain_label(domain: str, base_domain: str) -> str | None:
    """Return "shop" for "shop.<base_domain>", or None if domain is not a direct subdomain."""
    domain = domain.strip().lower().rstrip(".")
    suffix = "." + base_domain
    if not domain.endswith(suffix):
        return None
    label = domain[: -len(suffix)]
    return label if _SUBDOMAIN_LABEL.match(label) else None


@router.get(
    "/internal/tls-check",
    include_in_schema=False,
    dependencies=[Depends(require_direct_internal_call)],
)
def tls_check(
    request: Request, domain: str = "", session: Session = Depends(get_session)
) -> dict[str, str]:
    """Allow a certificate only for the main domain, www, or a real, non-deleted business."""
    base = get_settings().base_domain
    host = domain.strip().lower().rstrip(".")
    if host in (base, "www." + base):
        return {"allowed": host}
    label = subdomain_label(host, base)
    if label and label != "www":
        business = session.exec(
            select(Business).where(Business.slug == label, Business.deleted_at.is_(None))  # type: ignore[union-attr]
        ).first()
        if business is not None:
            return {"allowed": host}
    raise HTTPException(status_code=404)
