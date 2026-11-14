"""Request context: request ID, request log line, security headers and a last-resort error page."""

import logging
import time
import uuid

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response

from app.config import get_settings
from app.services.errors import render_error
from app.services.logs import request_id_var
from app.services.security import security_headers

logger = logging.getLogger("app.request")
_REQUEST_ID_HEADER = "X-Request-ID"


def _route_template(request: Request) -> str:
    """The matched route pattern (e.g. /o/{token}), so private tokens never reach the logs."""
    route = request.scope.get("route")
    return str(getattr(route, "path", "unmatched"))


class RequestContextMiddleware(BaseHTTPMiddleware):
    """Sets a request ID, logs one line per request, and adds security headers to every reply."""

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        """Handle one request."""
        request_id = uuid.uuid4().hex
        token = request_id_var.set(request_id)
        started = time.perf_counter()
        try:
            try:
                response = await call_next(request)
            except Exception:
                logger.exception("unhandled_error", extra={"route": _route_template(request)})
                response = render_error(request, "error.generic", 500)
            # Business sites set their own stricter headers; keep those, fill in the rest.
            for name, value in security_headers(get_settings()).items():
                response.headers.setdefault(name, value)
            response.headers[_REQUEST_ID_HEADER] = request_id
            logger.info(
                "request",
                extra={
                    "method": request.method,
                    "route": _route_template(request),
                    "status": response.status_code,
                    "ms": round((time.perf_counter() - started) * 1000, 1),
                    "client_ip": request.client.host if request.client else None,
                },
            )
            return response
        finally:
            request_id_var.reset(token)
