"""Rate limiting for public endpoints, keyed by the real client IP.

Uvicorn runs with --proxy-headers and FORWARDED_ALLOW_IPS set to the Caddy container only, so
request.client.host is the visitor's IP from X-Forwarded-For, and a forged header from anyone
else is ignored. Storage is in memory: fine for one app process; use a shared store if scaled.
"""

from collections.abc import Awaitable, Callable

from limits import RateLimitItem, parse
from limits.storage import MemoryStorage
from limits.strategies import MovingWindowRateLimiter
from starlette.requests import HTTPConnection, Request
from starlette.types import ASGIApp, Receive, Scope, Send

from app.services.errors import AppError, render_error

DEFAULT_LIMIT = "60/minute"
FORM_LIMIT = "5/minute"
CONTACT_HOURLY_LIMIT = "20/hour"
EXEMPT_PREFIXES = ("/health", "/internal/", "/static/", "/assets/", "/dev/preview/")

_storage = MemoryStorage()
_limiter = MovingWindowRateLimiter(_storage)


class RateLimited(AppError):
    """Too many requests from one client in the window."""

    def __init__(self) -> None:
        super().__init__("error.rate_limited", status_code=429)


def client_ip(connection: HTTPConnection) -> str:
    """The real client IP (already resolved from trusted proxy headers by uvicorn)."""
    return connection.client.host if connection.client else "unknown"


def hit(limit: RateLimitItem, scope_name: str, ip: str) -> bool:
    """Count one request; return False if the client is over the limit."""
    return _limiter.hit(limit, scope_name, ip)


def reset() -> None:
    """Clear all counters (used by tests)."""
    _storage.reset()


def rate_limit(limit: str) -> Callable[[Request], Awaitable[None]]:
    """FastAPI dependency factory for a tighter limit on one route, e.g. forms."""
    item = parse(limit)

    async def dependency(request: Request) -> None:
        name = f"route:{request.method}:{request.url.path}"
        if not hit(item, name, client_ip(request)):
            raise RateLimited()

    return dependency


class RateLimitMiddleware:
    """Applies DEFAULT_LIMIT per client IP to every public path."""

    def __init__(self, app: ASGIApp, limit: str = DEFAULT_LIMIT) -> None:
        self.app = app
        self.limit = parse(limit)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Reject over-limit HTTP requests with the friendly 429 page."""
        if scope["type"] != "http" or scope["path"].startswith(EXEMPT_PREFIXES):
            await self.app(scope, receive, send)
            return
        request = Request(scope, receive)
        if hit(self.limit, "global", client_ip(request)):
            await self.app(scope, receive, send)
            return
        response = render_error(request, RateLimited().key, 429)
        await response(scope, receive, send)
