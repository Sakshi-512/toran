"""Jinja2 templates with autoescape on, plus the i18n and CSRF helpers as globals."""

import hashlib
from functools import lru_cache
from pathlib import Path
from typing import Any

from fastapi import Request
from fastapi.templating import Jinja2Templates
from jinja2 import Environment, FileSystemLoader
from starlette.responses import Response

from app.config import get_settings
from app.services.i18n import SUPPORTED, normalise_language, pick_language, t
from app.services.security import CSRF_FIELD, SPAM_TRAP_FIELD, csrf_token

TEMPLATE_DIR = Path(__file__).resolve().parent / "templates"
STATIC_DIR = Path(__file__).resolve().parent / "static"


def static_url(name: str) -> str:
    """/static/<name>?v=<content hash>, so phones get new styles right after a change."""
    path = (STATIC_DIR / name).resolve()
    if not path.is_relative_to(STATIC_DIR) or not path.is_file():
        return f"/static/{name}"
    return f"/static/{name}?v={_content_hash(path, path.stat().st_mtime_ns)}"


@lru_cache(maxsize=32)
def _content_hash(path: Path, _mtime_ns: int) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()[:10]


def _build() -> Jinja2Templates:
    env = Environment(loader=FileSystemLoader(TEMPLATE_DIR), autoescape=True)
    env.globals.update(
        t=t,
        csrf_token=csrf_token,
        CSRF_FIELD=CSRF_FIELD,
        SPAM_TRAP_FIELD=SPAM_TRAP_FIELD,
        LANGUAGES=SUPPORTED,
        static_url=static_url,
    )
    return Jinja2Templates(env=env)


templates = _build()


def render(
    request: Request, name: str, context: dict[str, Any] | None = None, status_code: int = 200
) -> Response:
    """Render a template with the owner's language and public settings in the context."""
    settings = get_settings()
    chosen = normalise_language(request.query_params.get("lang"))
    if chosen and "session" in request.scope:
        request.session["lang"] = chosen  # her choice from the language links stays on every page
    base = {
        "lang": pick_language(request),
        "app_name": settings.app_name,
        "base_domain": settings.base_domain,
    }
    return templates.TemplateResponse(request, name, {**base, **(context or {})}, status_code)
