"""App factory: wires settings, logging, middleware, error handlers and routes. No logic here."""

from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from app.config import get_settings
from app.middleware import RequestContextMiddleware
from app.routes import health, pages
from app.services.errors import register_error_handlers
from app.services.logs import setup_logging
from app.services.ratelimit import RateLimitMiddleware
from app.services.security import session_options

STATIC_DIR = Path(__file__).resolve().parent / "static"


def create_app() -> FastAPI:
    """Build the FastAPI app. Run with: uvicorn --factory app.main:create_app."""
    settings = get_settings()
    setup_logging(settings.secret_values())

    app = FastAPI(
        title=settings.app_name,
        docs_url=None if settings.is_production else "/docs",
        redoc_url=None,
        openapi_url=None if settings.is_production else "/openapi.json",
    )
    # Added inner-first: the last one added is the outermost.
    app.add_middleware(RateLimitMiddleware)
    app.add_middleware(RequestContextMiddleware)
    app.add_middleware(SessionMiddleware, **session_options(settings))

    register_error_handlers(app)
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    app.include_router(health.router)
    app.include_router(pages.router)
    return app
