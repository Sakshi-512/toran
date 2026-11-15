"""Email over SMTP (our GoDaddy mailbox), behind a transport interface.

Port 465 uses SSL; 587 uses STARTTLS. Each attempt has a timeout and connection problems get one
retry; a rejected login is not retried. When SMTP is not configured (tests, local runs without
.env) messages go to an in-memory outbox and nothing leaves the machine. Logs never contain the
password, the subject or the body.
"""

import logging
import re
import smtplib
import ssl
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from email.message import EmailMessage
from email.utils import formataddr, make_msgid
from typing import Protocol

from app.config import Settings
from app.services.errors import AppError

logger = logging.getLogger(__name__)

TIMEOUT_SECONDS = 15.0
RETRY_DELAY_SECONDS = 2.0
SSL_PORT = 465
MAX_HEADER_CHARS = 150
_EMAIL = re.compile(r"^[^@\s<>\"',;]+@[^@\s<>\"',;]+\.[^@\s<>\"',;]{2,}$")
_RETRYABLE: tuple[type[Exception], ...] = (
    smtplib.SMTPServerDisconnected,
    smtplib.SMTPConnectError,
    ConnectionError,
    TimeoutError,
)


class MailError(AppError):
    """Email could not be sent; key is the friendly i18n message."""

    def __init__(self, key: str = "mail.failed") -> None:
        super().__init__(key, status_code=503)


class MailTransport(Protocol):
    """Delivers one finished message."""

    def deliver(self, message: EmailMessage) -> None:
        """Send the message or raise."""
        ...


@dataclass
class MemoryTransport:
    """Keeps messages in memory instead of sending them (tests and unconfigured setups)."""

    outbox: list[EmailMessage] = field(default_factory=list)

    def deliver(self, message: EmailMessage) -> None:
        """Store the message."""
        self.outbox.append(message)


@dataclass(frozen=True)
class SmtpTransport:
    """Sends through an SMTP server with SSL (port 465) or STARTTLS."""

    host: str
    port: int
    user: str
    password: str = field(repr=False)
    timeout: float = TIMEOUT_SECONDS

    def deliver(self, message: EmailMessage) -> None:
        """Connect, log in, send and disconnect."""
        context = ssl.create_default_context()
        if self.port == SSL_PORT:
            with smtplib.SMTP_SSL(
                self.host, self.port, timeout=self.timeout, context=context
            ) as server:
                server.login(self.user, self.password)
                server.send_message(message)
            return
        with smtplib.SMTP(self.host, self.port, timeout=self.timeout) as server:
            server.starttls(context=context)
            server.login(self.user, self.password)
            server.send_message(message)


outbox = MemoryTransport()


def is_configured(settings: Settings) -> bool:
    """True when SMTP host, user and password are all set."""
    return bool(settings.smtp_host and settings.smtp_user and settings.smtp_password)


def transport_for(settings: Settings) -> MailTransport:
    """SMTP when configured, otherwise the in-memory outbox."""
    if not is_configured(settings):
        return outbox
    assert settings.smtp_host and settings.smtp_user and settings.smtp_password  # noqa: S101
    return SmtpTransport(
        settings.smtp_host,
        settings.smtp_port,
        settings.smtp_user,
        settings.smtp_password.get_secret_value(),
    )


def clean_header(value: str, limit: int = MAX_HEADER_CHARS) -> str:
    """One line, no control characters, capped: form input can never add headers."""
    return re.sub(r"[\x00-\x1f\x7f]+", " ", value).strip()[:limit]


def valid_email(value: str) -> bool:
    """A plain address like name@example.com (no display names or lists)."""
    return len(value) <= 254 and bool(_EMAIL.match(value))


def build_message(
    settings: Settings, to: str, subject: str, body: str, reply_to: str | None = None
) -> EmailMessage:
    """A plain-text UTF-8 message from our mailbox."""
    sender = settings.smtp_user or f"noreply@{settings.base_domain}"
    message = EmailMessage()
    message["From"] = formataddr((clean_header(settings.app_name), sender))
    message["To"] = to
    message["Subject"] = clean_header(subject)
    message["Message-ID"] = make_msgid(domain=settings.base_domain)
    if reply_to and valid_email(reply_to.strip()):
        message["Reply-To"] = reply_to.strip()
    message.set_content(body, charset="utf-8")
    return message


def _deliver_with_retry(
    transport: MailTransport, message: EmailMessage, sleep: Callable[[float], None]
) -> None:
    for attempt in (1, 2):
        try:
            transport.deliver(message)
            return
        except smtplib.SMTPAuthenticationError as exc:
            raise MailError("mail.failed") from exc
        except _RETRYABLE as exc:
            if attempt == 2:
                raise MailError("mail.failed") from exc
            logger.warning("mail_retry", extra={"error": type(exc).__name__})
            sleep(RETRY_DELAY_SECONDS)
        except (smtplib.SMTPException, OSError) as exc:
            raise MailError("mail.failed") from exc


def send_mail(
    settings: Settings,
    *,
    to: str,
    subject: str,
    body: str,
    kind: str,
    reply_to: str | None = None,
    transport: MailTransport | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> str:
    """Send one email; returns its Message-ID. Raises MailError with a friendly key."""
    message = build_message(settings, to, subject, body, reply_to)
    try:
        _deliver_with_retry(transport or transport_for(settings), message, sleep)
    except MailError as exc:
        logger.warning("mail_failed", extra={"kind": kind, "error": type(exc.__cause__).__name__})
        raise
    logger.info(
        "mail_sent",
        extra={"kind": kind, "message_id": message["Message-ID"], "smtp": is_configured(settings)},
    )
    return str(message["Message-ID"])
