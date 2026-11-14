"""CSRF tokens, spam trap, session cookie options and security headers."""

import hmac
import secrets
from typing import Any

from itsdangerous import BadSignature, URLSafeTimedSerializer
from starlette.requests import Request

from app.config import Settings, get_settings
from app.services.errors import AppError

CSRF_FIELD = "csrf_token"
CSRF_HEADER = "x-csrf-token"
CSRF_MAX_AGE_SECONDS = 8 * 60 * 60
SPAM_TRAP_FIELD = "website"
SESSION_COOKIE = "vsa_session"
_SESSION_CSRF_KEY = "csrf"


class CsrfError(AppError):
    """The form's CSRF token is missing, expired or does not match the session."""

    def __init__(self) -> None:
        super().__init__("error.csrf", status_code=403)


def session_options(settings: Settings) -> dict[str, Any]:
    """Options for Starlette's SessionMiddleware: HTTP-only always, Secure in production."""
    return {
        "secret_key": settings.secret_key.get_secret_value(),
        "session_cookie": SESSION_COOKIE,
        "same_site": "lax",
        "https_only": settings.cookie_secure,
        "max_age": 14 * 24 * 60 * 60,
    }


def _serializer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(get_settings().secret_key.get_secret_value(), salt="csrf")


def csrf_token(request: Request) -> str:
    """Return a signed CSRF token tied to this visitor's session (created on first use)."""
    session_secret = request.session.get(_SESSION_CSRF_KEY)
    if not session_secret:
        session_secret = secrets.token_urlsafe(32)
        request.session[_SESSION_CSRF_KEY] = session_secret
    return _serializer().dumps(session_secret)


def check_csrf_token(request: Request, token: str | None) -> None:
    """Raise CsrfError unless token is valid, unexpired and matches the session."""
    session_secret = request.session.get(_SESSION_CSRF_KEY)
    if not token or not session_secret:
        raise CsrfError()
    try:
        value = _serializer().loads(token, max_age=CSRF_MAX_AGE_SECONDS)
    except BadSignature as exc:
        raise CsrfError() from exc
    if not isinstance(value, str) or not hmac.compare_digest(value, session_secret):
        raise CsrfError()


async def verify_csrf(request: Request) -> None:
    """FastAPI dependency for every state-changing form: checks the form field or header."""
    token = request.headers.get(CSRF_HEADER)
    if token is None:
        form = await request.form()
        field = form.get(CSRF_FIELD)
        token = field if isinstance(field, str) else None
    check_csrf_token(request, token)


def is_spam(form: dict[str, Any]) -> bool:
    """True if the hidden spam-trap field was filled in (only bots do that)."""
    return bool(str(form.get(SPAM_TRAP_FIELD, "")).strip())


CONTENT_SECURITY_POLICY = "; ".join(
    [
        "default-src 'self'",
        "img-src 'self' data:",
        "style-src 'self'",
        "script-src 'self'",
        "font-src 'self'",
        "connect-src 'self'",
        "media-src 'self' blob:",
        "object-src 'none'",
        "base-uri 'self'",
        "form-action 'self'",
        "frame-ancestors 'none'",
    ]
)


def security_headers(settings: Settings) -> dict[str, str]:
    """Headers added to every response from the app and every business site."""
    headers = {
        "Content-Security-Policy": CONTENT_SECURITY_POLICY,
        "X-Content-Type-Options": "nosniff",
        "Referrer-Policy": "strict-origin-when-cross-origin",
        "X-Frame-Options": "DENY",
        "Permissions-Policy": "camera=(self), microphone=(self), geolocation=()",
        "Cross-Origin-Opener-Policy": "same-origin",
    }
    if settings.is_production:
        headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    return headers


def site_security_headers(
    settings: Settings, form_origin: str, framable: bool = False
) -> dict[str, str]:
    """Headers for business sites: no scripts at all, forms may only post to the main app.

    framable=True allows same-origin framing, used only by the development theme gallery.
    """
    ancestors = "'self'" if framable else "'none'"
    csp = "; ".join(
        [
            "default-src 'none'",
            "style-src 'self'",
            "img-src 'self' data:",
            "font-src 'self'",
            f"form-action {form_origin}",
            "base-uri 'none'",
            f"frame-ancestors {ancestors}",
        ]
    )
    headers = {
        **security_headers(settings),
        "Content-Security-Policy": csp,
        "X-Frame-Options": "SAMEORIGIN" if framable else "DENY",
    }
    return headers
