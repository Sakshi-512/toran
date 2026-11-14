"""Friendly error pages in the owner's language. Stack traces go to the logs, never to users."""

import logging

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import Response
from starlette.exceptions import HTTPException as StarletteHTTPException

logger = logging.getLogger(__name__)


class AppError(Exception):
    """An expected error with an i18n message key and an HTTP status."""

    def __init__(self, key: str, status_code: int = 400) -> None:
        super().__init__(key)
        self.key = key
        self.status_code = status_code


class SpendCapReached(AppError):
    """Today's Claude spend cap is reached; Claude calls stop until tomorrow (Asia/Kolkata)."""

    def __init__(self, spent_usd: float, cap_usd: float) -> None:
        super().__init__("error.spend_cap", status_code=503)
        self.spent_usd = spent_usd
        self.cap_usd = cap_usd

    def __str__(self) -> str:
        return f"Daily Claude spend cap reached: ${self.spent_usd:.4f} of ${self.cap_usd:.2f}"


_STATUS_KEYS = {403: "error.forbidden", 404: "error.not_found", 405: "error.not_found"}


def render_error(request: Request, key: str, status_code: int) -> Response:
    """Render the friendly error page (HTML) or a small JSON body for API callers."""
    from app.services.i18n import pick_language, t
    from app.templating import render

    if "application/json" in request.headers.get("accept", ""):
        from fastapi.responses import JSONResponse

        message = t(key, pick_language(request))
        return JSONResponse({"error": key, "message": message}, status_code=status_code)
    return render(request, "app/error.html", {"message_key": key}, status_code=status_code)


async def _app_error(request: Request, exc: Exception) -> Response:
    assert isinstance(exc, AppError)  # noqa: S101 - registered only for AppError
    logger.info("app_error", extra={"key": exc.key, "status": exc.status_code})
    return render_error(request, exc.key, exc.status_code)


async def _http_error(request: Request, exc: Exception) -> Response:
    assert isinstance(exc, StarletteHTTPException)  # noqa: S101
    key = _STATUS_KEYS.get(exc.status_code, "error.generic")
    return render_error(request, key, exc.status_code)


async def _validation_error(request: Request, exc: Exception) -> Response:
    logger.info("validation_error", extra={"path": request.url.path})
    return render_error(request, "error.invalid_input", 422)


def register_error_handlers(app: FastAPI) -> None:
    """Attach friendly handlers for expected errors. Unexpected ones are caught by middleware."""
    app.add_exception_handler(AppError, _app_error)
    app.add_exception_handler(StarletteHTTPException, _http_error)
    app.add_exception_handler(RequestValidationError, _validation_error)
