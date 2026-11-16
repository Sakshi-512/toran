"""Owner access: an unguessable private link plus a 4-digit PIN, both stored only as hashes.

Five wrong PINs lock the link for 15 minutes. A correct PIN signs the owner in with the session
cookie and remembers her device for 30 days (a separate secure, HTTP-only cookie whose token is
stored only as a hash, so it can be revoked). A remembered device signs her in again, but never
counts as a recent PIN: payment details and deleting the site need the PIN again
(require_recent_pin).
"""

import hashlib
import hmac
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal

from sqlmodel import Session, select
from starlette.requests import Request
from starlette.responses import Response

from app.config import Settings
from app.db import get_engine
from app.models import Business, OwnerDevice, utcnow
from app.services.errors import AppError

MAX_PIN_FAILURES = 5
LOCK_MINUTES = 15
SESSION_HOURS = 12
RECENT_PIN_MINUTES = 5
DEVICE_DAYS = 30
DEVICE_COOKIE = "owner_device"
_SCRYPT = {"n": 2**14, "r": 8, "p": 1, "dklen": 32}
_OWNER_KEY = "owner_business_id"
_SIGNED_IN_KEY = "owner_signed_in_at"
_PIN_AT_KEY = "owner_pin_at"

PinResult = Literal["ok", "wrong", "locked"]


@dataclass(frozen=True)
class OwnerAccess:
    """Shown to the owner once, never stored in plain form."""

    link_token: str
    pin: str


def hash_token(token: str) -> str:
    """SHA-256 of a random token (tokens are long and random, so no salt is needed)."""
    return hashlib.sha256(token.encode()).hexdigest()


def new_pin() -> str:
    """A random 4-digit PIN."""
    return f"{secrets.randbelow(10_000):04d}"


def hash_pin(pin: str) -> str:
    """Salted scrypt hash, stored as scrypt$<salt>$<hash>."""
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(pin.encode(), salt=salt, **_SCRYPT)
    return f"scrypt${salt.hex()}${digest.hex()}"


def pin_matches(pin: str, stored: str) -> bool:
    """Constant-time check of a PIN against its stored hash."""
    try:
        scheme, salt, digest = stored.split("$")
    except ValueError:
        return False
    if scheme != "scrypt":
        return False
    check = hashlib.scrypt(pin.encode(), salt=bytes.fromhex(salt), **_SCRYPT)
    return hmac.compare_digest(check.hex(), digest)


def issue_access(business: Business) -> OwnerAccess:
    """Give a business a new private link and PIN; returns the plain values to show once."""
    access = OwnerAccess(secrets.token_urlsafe(32), new_pin())
    business.owner_link_hash = hash_token(access.link_token)
    business.pin_hash = hash_pin(access.pin)
    business.pin_failures = 0
    business.pin_locked_until = None
    return access


def business_for_link(session: Session, token: str) -> Business | None:
    """The business a private link belongs to (never a deleted one)."""
    if not token or len(token) > 100:
        return None
    statement = select(Business).where(Business.owner_link_hash == hash_token(token))
    business = session.exec(statement).first()
    return None if business is None or business.deleted_at is not None else business


def _aware(moment: datetime | None) -> datetime | None:
    return moment.replace(tzinfo=UTC) if moment and moment.tzinfo is None else moment


def check_pin(
    session: Session, business: Business, pin: str, now: datetime | None = None
) -> PinResult:
    """Check a PIN, counting failures and locking after MAX_PIN_FAILURES."""
    moment = now or utcnow()
    locked_until = _aware(business.pin_locked_until)
    if locked_until and locked_until > moment:
        return "locked"
    if (
        business.pin_hash
        and pin.isdigit()
        and len(pin) == 4
        and pin_matches(pin, business.pin_hash)
    ):
        business.pin_failures, business.pin_locked_until = 0, None
        result: PinResult = "ok"
    else:
        business.pin_failures += 1
        result = "wrong"
        if business.pin_failures >= MAX_PIN_FAILURES:
            business.pin_failures = 0
            business.pin_locked_until = moment + timedelta(minutes=LOCK_MINUTES)
            result = "locked"
    session.add(business)
    session.commit()
    return result


def sign_in(request: Request, business_id: int, pin_entered: bool = True) -> None:
    """Remember the signed-in owner in the session cookie."""
    now = utcnow().timestamp()
    request.session[_OWNER_KEY] = business_id
    request.session[_SIGNED_IN_KEY] = now
    if pin_entered:
        request.session[_PIN_AT_KEY] = now
    else:
        request.session.pop(_PIN_AT_KEY, None)


def remember_device(response: Response, business_id: int, settings: Settings) -> None:
    """After a correct PIN: keep this device signed in for 30 days."""
    token = secrets.token_urlsafe(32)
    with Session(get_engine()) as session:
        session.add(
            OwnerDevice(
                business_id=business_id,
                token_hash=hash_token(token),
                expires_at=utcnow() + timedelta(days=DEVICE_DAYS),
            )
        )
        session.commit()
    response.set_cookie(
        DEVICE_COOKIE,
        token,
        max_age=DEVICE_DAYS * 86400,
        path="/",
        httponly=True,
        secure=settings.cookie_secure,
        samesite="lax",
    )


def device_owner(request: Request) -> int | None:
    """The business a remembered-device cookie belongs to, if it is valid and not revoked."""
    token = request.cookies.get(DEVICE_COOKIE, "")
    if not token or len(token) > 100:
        return None
    with Session(get_engine()) as session:
        device = session.exec(
            select(OwnerDevice).where(OwnerDevice.token_hash == hash_token(token))
        ).first()
        now = utcnow()
        if device is None or device.revoked_at is not None:
            return None
        expires = device.expires_at
        if (expires if expires.tzinfo else expires.replace(tzinfo=UTC)) <= now:
            return None
        business = session.get(Business, device.business_id)
        if business is None or business.deleted_at is not None:
            return None
        device.last_used_at = now
        session.add(device)
        session.commit()
        return device.business_id


def forget_device(request: Request, response: Response) -> None:
    """Sign-out: revoke this device and clear its cookie."""
    token = request.cookies.get(DEVICE_COOKIE, "")
    if token:
        with Session(get_engine()) as session:
            device = session.exec(
                select(OwnerDevice).where(OwnerDevice.token_hash == hash_token(token))
            ).first()
            if device is not None:
                device.revoked_at = utcnow()
                session.add(device)
                session.commit()
    response.delete_cookie(DEVICE_COOKIE, path="/")


def sign_out(request: Request) -> None:
    """Forget the owner."""
    for key in (_OWNER_KEY, _SIGNED_IN_KEY, _PIN_AT_KEY):
        request.session.pop(key, None)


def current_owner(request: Request) -> int | None:
    """The signed-in owner's business id, if the sign-in is still fresh."""
    business_id = request.session.get(_OWNER_KEY)
    signed_in = request.session.get(_SIGNED_IN_KEY)
    if not isinstance(business_id, int) or not isinstance(signed_in, int | float):
        remembered = device_owner(request)
        if remembered is not None:
            sign_in(request, remembered, pin_entered=False)
        return remembered
    if utcnow().timestamp() - signed_in > SESSION_HOURS * 3600:
        sign_out(request)
        remembered = device_owner(request)
        if remembered is not None:
            sign_in(request, remembered, pin_entered=False)
        return remembered
    return business_id


def require_owner(request: Request) -> int:
    """FastAPI dependency: the signed-in owner's business id, or a friendly sign-in message."""
    business_id = current_owner(request)
    if business_id is None:
        raise AppError("owner.signin_required", status_code=401)
    return business_id


def require_recent_pin(request: Request) -> int:
    """For sensitive changes: the PIN must have been entered in the last few minutes."""
    business_id = require_owner(request)
    pin_at = request.session.get(_PIN_AT_KEY)
    if (
        not isinstance(pin_at, int | float)
        or utcnow().timestamp() - pin_at > RECENT_PIN_MINUTES * 60
    ):
        raise AppError("owner.pin_again", status_code=403)
    return business_id
