"""Rate limiting, keyed by the real client IP (or the signed-in owner).

Uvicorn runs with --proxy-headers and FORWARDED_ALLOW_IPS set to the Caddy container only, so
request.client.host is the visitor's IP from X-Forwarded-For, and a forged header from anyone
else is ignored. Storage is in memory: fine for one app process; use a shared store if scaled.

Two global limits:
- signed-in owner pages, the auto-refreshing waiting page and job polling: OWNER_LIMIT, per
  session (the owner's business, or the IP before sign-in). Normal use, including previews and
  auto-refresh, never comes close; it still stops a misused session.
- everything else that is public: DEFAULT_LIMIT per IP.
Forms add their own tighter limits (rate_limit). Every limit reports when she can try again.
"""

import math
import re
import time
from collections.abc import Awaitable, Callable

from limits import RateLimitItem, parse
from limits.storage import MemoryStorage
from limits.strategies import MovingWindowRateLimiter
from starlette.requests import HTTPConnection, Request
from starlette.types import ASGIApp, Receive, Scope, Send

from app.services.errors import AppError

DEFAULT_LIMIT = "60/minute"
OWNER_LIMIT = "600/minute"
FORM_LIMIT = "5/minute"
CONTACT_HOURLY_LIMIT = "20/hour"
INTAKE_HOURLY_LIMIT = "10/hour"
EXEMPT_PREFIXES = ("/health", "/internal/", "/static/", "/assets/", "/dev/preview/")
_OWNER_PATHS = re.compile(r"^/(owner(/|$)|api/jobs/|start/[^/]+/wait$)")

_storage = MemoryStorage()
_limiter = MovingWindowRateLimiter(_storage)


class RateLimited(AppError):
    """Too many requests in the window; retry_after says when she can try again."""

    def __init__(self, retry_after: int = 60) -> None:
        super().__init__("error.rate_limited", status_code=429)
        self.retry_after = max(1, retry_after)


def client_ip(connection: HTTPConnection) -> str:
    """The real client IP (already resolved from trusted proxy headers by uvicorn)."""
    return connection.client.host if connection.client else "unknown"


def hit(limit: RateLimitItem, scope_name: str, ip: str) -> bool:
    """Count one request; return False if the client is over the limit."""
    return _limiter.hit(limit, scope_name, ip)


def seconds_until_free(limit: RateLimitItem, scope_name: str, ip: str) -> int:
    """Seconds until the oldest counted request leaves the window."""
    reset_at, _remaining = _limiter.get_window_stats(limit, scope_name, ip)
    return max(1, math.ceil(reset_at - time.time()))


def reset() -> None:
    """Clear all counters (used by tests)."""
    _storage.reset()


def check(request: Request, *limits: str) -> None:
    """Count this request against each limit; raise RateLimited with the wait if over one."""
    name = f"route:{request.method}:{request.url.path}"
    for limit in limits:
        item = parse(limit)
        if not hit(item, name, client_ip(request)):
            raise RateLimited(seconds_until_free(item, name, client_ip(request)))


def rate_limit(limit: str) -> Callable[[Request], Awaitable[None]]:
    """FastAPI dependency for a tighter limit on one route (owner actions, enquiries)."""

    async def dependency(request: Request) -> None:
        check(request, limit)

    return dependency


def _owner_key(scope: Scope, request: Request) -> str:
    session = scope.get("session") or {}
    owner = session.get("owner_business_id")
    return f"owner:{owner}" if isinstance(owner, int) else f"ip:{client_ip(request)}"


class RateLimitMiddleware:
    """Applies OWNER_LIMIT to owner pages and polling, and DEFAULT_LIMIT to everything else."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app
        self.default = parse(DEFAULT_LIMIT)
        self.owner = parse(OWNER_LIMIT)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Reject over-limit requests with the friendly countdown page."""
        path = scope.get("path", "")
        if scope["type"] != "http" or path.startswith(EXEMPT_PREFIXES):
            await self.app(scope, receive, send)
            return
        request = Request(scope, receive)
        if _OWNER_PATHS.match(path):
            item, name, key = self.owner, "owner", _owner_key(scope, request)
        else:
            item, name, key = self.default, "global", client_ip(request)
        if hit(item, name, key):
            await self.app(scope, receive, send)
            return
        from app.services.errors import render_rate_limited

        response = render_rate_limited(request, seconds_until_free(item, name, key))
        await response(scope, receive, send)
