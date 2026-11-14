"""App factory: wires settings, logging, middleware, error handlers and routes. No logic here."""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from app.config import get_settings
from app.db import get_engine
from app.middleware import RequestContextMiddleware
from app.routes import dev, enquiry, health, pages
from app.services.errors import register_error_handlers
from app.services.logs import setup_logging
from app.services.ratelimit import RateLimitMiddleware
from app.services.retention import retention_loop
from app.services.security import session_options
from app.services.sites import SiteHostMiddleware

STATIC_DIR = Path(__file__).resolve().parent / "static"


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    """Start the daily retention cleanup with the app and stop it on shutdown."""
    settings = get_settings()
    task = asyncio.create_task(retention_loop(get_engine(), settings.log_dir))
    try:
        yield
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task


def create_app() -> FastAPI:
    """Build the FastAPI app. Run with: uvicorn --factory app.main:create_app."""
    settings = get_settings()
    setup_logging(settings.secret_values(), settings.log_dir)

    app = FastAPI(
        title=settings.app_name,
        docs_url=None if settings.is_production else "/docs",
        redoc_url=None,
        openapi_url=None if settings.is_production else "/openapi.json",
        lifespan=lifespan,
    )
    # Added inner-first: the last one added is the outermost.
    app.add_middleware(SiteHostMiddleware)
    app.add_middleware(RateLimitMiddleware)
    app.add_middleware(RequestContextMiddleware)
    app.add_middleware(SessionMiddleware, **session_options(settings))

    register_error_handlers(app)
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    app.include_router(health.router)
    app.include_router(pages.router)
    app.include_router(enquiry.router)
    if not settings.is_production:
        app.include_router(dev.router)
    return app
