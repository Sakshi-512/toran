"""Request body limits, applied before any form or upload is read.

Caddy refuses bodies over the same size first (request_body max_size in the Caddyfile). This
middleware is the app's own guard: a declared Content-Length over the limit is refused at once,
and a body sent without one (chunked) is counted as it arrives and refused when it passes the
limit, so an upload can never fill memory or disk. Forms also accept at most MAX_FILES files.
The largest real request is the confirmation cards with up to 8 photos of 8 MB at most.
"""

from starlette.datastructures import Headers
from starlette.requests import Request
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.services.errors import AppError, render_error

MAX_REQUEST_BYTES = 70 * 1024 * 1024
MAX_FILES = 12
TOO_LARGE_KEY = "upload.too_large"


class BodyTooLarge(AppError):
    """The request body is over MAX_REQUEST_BYTES."""

    def __init__(self) -> None:
        super().__init__(TOO_LARGE_KEY, status_code=413)


class BodyLimitMiddleware:
    """Refuses request bodies over MAX_REQUEST_BYTES with the friendly "too large" page."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Check the declared size, then count the bytes actually received."""
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        limit = MAX_REQUEST_BYTES
        declared = Headers(scope=scope).get("content-length", "")
        if declared.isdigit() and int(declared) > limit:
            response = render_error(Request(scope), TOO_LARGE_KEY, 413)
            await response(scope, receive, send)
            return
        received = 0

        async def limited_receive() -> Message:
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > limit:
                    raise BodyTooLarge()
            return message

        await self.app(scope, limited_receive, send)
