"""Jinja2 templates with autoescape on, plus the i18n and CSRF helpers as globals."""

from pathlib import Path
from typing import Any

from fastapi import Request
from fastapi.templating import Jinja2Templates
from jinja2 import Environment, FileSystemLoader
from starlette.responses import Response

from app.config import get_settings
from app.services.i18n import SUPPORTED, pick_language, t
from app.services.security import CSRF_FIELD, SPAM_TRAP_FIELD, csrf_token

TEMPLATE_DIR = Path(__file__).resolve().parent / "templates"


def _build() -> Jinja2Templates:
    env = Environment(loader=FileSystemLoader(TEMPLATE_DIR), autoescape=True)
    env.globals.update(
        t=t,
        csrf_token=csrf_token,
        CSRF_FIELD=CSRF_FIELD,
        SPAM_TRAP_FIELD=SPAM_TRAP_FIELD,
        LANGUAGES=SUPPORTED,
    )
    return Jinja2Templates(env=env)


templates = _build()


def render(
    request: Request, name: str, context: dict[str, Any] | None = None, status_code: int = 200
) -> Response:
    """Render a template with the owner's language and public settings in the context."""
    settings = get_settings()
    base = {
        "lang": pick_language(request),
        "app_name": settings.app_name,
        "base_domain": settings.base_domain,
    }
    return templates.TemplateResponse(request, name, {**base, **(context or {})}, status_code)
