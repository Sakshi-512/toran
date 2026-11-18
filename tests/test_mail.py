import asyncio
import logging
import smtplib
import time
from collections.abc import Callable
from email.message import EmailMessage

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.engine import Engine
from sqlmodel import Session, select

from app.config import get_settings
from app.models import Business, Enquiry
from app.services import mailer
from app.services.alerts import AlertHandler
from app.services.jobs import JobWorker
from app.services.mail_jobs import EMAIL_JOB, email_runner
from app.services.mailer import (
    MailError,
    MemoryTransport,
    SmtpTransport,
    build_message,
    send_mail,
    transport_for,
)
from app.services.security import CSRF_FIELD

TEAM = "team@example.test"


class FlakyTransport:
    def __init__(self, errors: list[Exception]) -> None:
        self.errors = errors
        self.sent: list[EmailMessage] = []
        self.calls = 0

    def deliver(self, message: EmailMessage) -> None:
        self.calls += 1
        if self.errors:
            raise self.errors.pop(0)
        self.sent.append(message)


def _drain(engine: Engine) -> None:
    """Run queued email jobs (the app's own worker may also pick them up)."""
    worker = JobWorker(engine, {EMAIL_JOB: email_runner(engine, get_settings())})
    asyncio.run(worker.run_until_idle())


def _wait_for_outbox(count: int, timeout: float = 5.0) -> list[EmailMessage]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        found = [m for m in mailer.outbox.outbox if "error" not in str(m["Subject"])]
        if len(found) >= count:
            return found
        time.sleep(0.05)
    return [m for m in mailer.outbox.outbox if "error" not in str(m["Subject"])]


# --- mailer -----------------------------------------------------------------------------


def test_unconfigured_smtp_uses_memory_outbox() -> None:
    assert transport_for(get_settings()) is mailer.outbox


def test_configured_smtp_uses_ssl_transport(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SMTP_HOST", "smtp.example.test")
    monkeypatch.setenv("SMTP_USER", "hello@example.test")
    get_settings.cache_clear()
    transport = transport_for(get_settings())
    assert isinstance(transport, SmtpTransport)
    assert transport.port == 465 and transport.timeout == 15.0
    secret = get_settings().smtp_password
    assert secret is not None and secret.get_secret_value() not in repr(transport)


def test_ssl_transport_logs_in_and_sends(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, object]] = []

    class FakeSSL:
        def __init__(self, host: str, port: int, timeout: float, context: object) -> None:
            calls.append(("connect", (host, port, timeout)))

        def __enter__(self) -> "FakeSSL":
            return self

        def __exit__(self, *_: object) -> None:
            calls.append(("quit", None))

        def login(self, user: str, password: str) -> None:
            calls.append(("login", user))

        def send_message(self, message: EmailMessage) -> None:
            calls.append(("send", message["To"]))

    monkeypatch.setattr(smtplib, "SMTP_SSL", FakeSSL)
    SmtpTransport("smtp.example.test", 465, "hello@example.test", "pw").deliver(
        build_message(get_settings(), TEAM, "Hi", "Body")
    )
    assert calls == [
        ("connect", ("smtp.example.test", 465, 15.0)),
        ("login", "hello@example.test"),
        ("send", TEAM),
        ("quit", None),
    ]


def test_message_headers_and_utf8() -> None:
    message = build_message(
        get_settings(),
        TEAM,
        "Test App contact form: Kavita",
        "नमस्ते, ₹450 cake?",
        reply_to="kavita@example.test",
    )
    assert message["To"] == TEAM
    assert message["Reply-To"] == "kavita@example.test"
    assert message["Subject"] == "Test App contact form: Kavita"
    assert "Test App" in message["From"]
    assert "नमस्ते" in message.get_content()


def test_header_injection_is_removed() -> None:
    message = build_message(
        get_settings(),
        TEAM,
        "Hello\r\nBcc: victim@example.test",
        "x",
        reply_to="a@example.test\r\nBcc: victim@example.test",
    )
    assert "\n" not in message["Subject"] and "\r" not in message["Subject"]
    assert message["Bcc"] is None
    assert message["Reply-To"] is None  # not a plain address, so ignored


def test_one_retry_after_dropped_connection() -> None:
    transport = FlakyTransport([smtplib.SMTPServerDisconnected("gone")])
    send_mail(
        get_settings(),
        to=TEAM,
        subject="s",
        body="b",
        kind="test",
        transport=transport,
        sleep=lambda _s: None,
    )
    assert transport.calls == 2 and len(transport.sent) == 1


def test_gives_up_after_one_retry() -> None:
    transport = FlakyTransport([TimeoutError(), TimeoutError(), TimeoutError()])
    with pytest.raises(MailError) as caught:
        send_mail(
            get_settings(),
            to=TEAM,
            subject="s",
            body="b",
            kind="test",
            transport=transport,
            sleep=lambda _s: None,
        )
    assert transport.calls == 2
    assert caught.value.key == "mail.failed"


def test_rejected_login_is_not_retried() -> None:
    transport = FlakyTransport([smtplib.SMTPAuthenticationError(535, b"bad credentials")])
    with pytest.raises(MailError):
        send_mail(
            get_settings(),
            to=TEAM,
            subject="s",
            body="b",
            kind="test",
            transport=transport,
            sleep=lambda _s: None,
        )
    assert transport.calls == 1


def test_password_subject_and_body_never_logged(capsys: pytest.CaptureFixture[str]) -> None:
    from app.services.logs import setup_logging

    setup_logging(get_settings().secret_values())
    secret = get_settings().smtp_password
    assert secret is not None
    password = secret.get_secret_value()
    transport = FlakyTransport([smtplib.SMTPServerDisconnected("gone")])
    send_mail(
        get_settings(),
        to=TEAM,
        subject="Secret subject Kavita",
        kind="test",
        body="private body text 9000012345",
        transport=transport,
        sleep=lambda _s: None,
    )
    logged = "".join(capsys.readouterr())
    assert "mail_sent" in logged
    for leaked in (password, "Secret subject", "private body text", "9000012345"):
        assert leaked not in logged


def test_mail_errors_have_friendly_text() -> None:
    from app.services.i18n import t

    assert "try again" in t("mail.failed", "en")
    assert t("mail.failed", "hi") != t("mail.failed", "en")


# --- contact form -------------------------------------------------------------------------


def _token(client: TestClient, lang: str = "en") -> str:
    html = client.get(f"/?lang={lang}").text
    return html.split(f'name="{CSRF_FIELD}" value="')[1].split('"')[0]


def _contact(client: TestClient, **fields: str) -> object:
    data = {
        "name": "Kavita",
        "contact": "kavita@example.test",
        "message": "Hello Toran!",
        "website": "",
        CSRF_FIELD: _token(client),
    }
    data.update(fields)
    return client.post("/contact", data=data, follow_redirects=False)


def test_home_page_has_contact_form_in_three_languages(client: TestClient) -> None:
    for lang, label in (("en", "Contact us"), ("hi", "हमसे संपर्क करें"), ("mr", "आमच्याशी संपर्क साधा")):
        html = client.get(f"/?lang={lang}").text
        assert label in html and 'action="/contact"' in html and 'name="website"' in html


def test_contact_form_saves_and_emails_team(client: TestClient, engine: Engine) -> None:
    response = _contact(client)
    assert response.status_code == 303  # type: ignore[attr-defined]
    assert response.headers["location"] == "/?sent=1#contact"  # type: ignore[attr-defined]
    _drain(engine)
    [message] = _wait_for_outbox(1)
    assert message["To"] == TEAM
    assert message["Subject"] == "Test App contact form: Kavita"
    assert message["Reply-To"] == "kavita@example.test"
    assert "Hello Toran!" in message.get_content()
    with Session(engine) as session:
        saved = session.exec(select(Enquiry)).one()
    assert saved.business_id is None and saved.email == "kavita@example.test"
    assert saved.notified_at is not None


def test_contact_with_phone_has_no_reply_to(client: TestClient, engine: Engine) -> None:
    _contact(client, contact="90000 12345")
    _drain(engine)
    [message] = _wait_for_outbox(1)
    assert message["Reply-To"] is None
    assert "+919000012345" in message.get_content()


def test_thank_you_message_in_owner_language(client: TestClient) -> None:
    assert "Thank you! Your message has been sent." in client.get("/?sent=1").text
    assert "धन्यवाद! तुमचा संदेश आम्हाला मिळाला आहे." in client.get("/?sent=1&lang=mr").text


def test_contact_requires_csrf(client: TestClient, engine: Engine) -> None:
    client.get("/")
    response = client.post(
        "/contact",
        data={"name": "A", "contact": "a@example.test", "message": "x"},
        follow_redirects=False,
    )
    assert response.status_code == 403
    with Session(engine) as session:
        assert session.exec(select(Enquiry)).all() == []


def test_contact_spam_trap_drops_silently(client: TestClient, engine: Engine) -> None:
    response = _contact(client, website="http://spam.example")
    assert response.status_code == 303  # type: ignore[attr-defined]
    _drain(engine)
    with Session(engine) as session:
        assert session.exec(select(Enquiry)).all() == []
    assert _wait_for_outbox(1, timeout=0.3) == []


@pytest.mark.parametrize(
    "bad", [{"contact": "not-an-email@"}, {"contact": "12345"}, {"name": ""}, {"message": " "}]
)
def test_contact_validation(client: TestClient, bad: dict[str, str]) -> None:
    assert _contact(client, **bad).status_code == 422  # type: ignore[attr-defined]


def test_contact_rate_limits_per_minute_and_per_hour(client: TestClient) -> None:
    codes = [_contact(client).status_code for _ in range(6)]  # type: ignore[attr-defined]
    assert codes == [303] * 5 + [429]


def test_contact_hourly_limit(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    from app.services import ratelimit

    original = ratelimit.hit

    def only_hourly(limit, scope_name, ip):  # type: ignore[no-untyped-def]
        # Skip per-minute limits so 21 posts in a few seconds reach the hourly limit.
        return True if limit.GRANULARITY.name == "minute" else original(limit, scope_name, ip)

    monkeypatch.setattr(ratelimit, "hit", only_hourly)
    codes = [_contact(client).status_code for _ in range(21)]  # type: ignore[attr-defined]
    assert codes[:20] == [303] * 20 and codes[20] == 429


# --- business enquiry notifications ---------------------------------------------------------


def test_enquiry_notification_email(
    client: TestClient, engine: Engine, seed_sample: Callable[[str], Business]
) -> None:
    seed_sample("sunita-beauty-parlour")
    client.post(
        "/api/enquiry/sunita-beauty-parlour",
        data={
            "name": "Kavita",
            "phone": "90000 12345",
            "message": "Is Sunday free?",
            "lang": "en",
            "website": "",
        },
        headers={"Origin": "https://sunita-beauty-parlour.example.test"},
        follow_redirects=False,
    )
    _drain(engine)
    [message] = _wait_for_outbox(1)
    assert message["Subject"] == "[Sample] New enquiry for Sunita Beauty Parlour"
    body = message.get_content()
    assert "Sunita Beauty Parlour" in body and "Is Sunday free?" in body
    assert "+919000012345" in body and "Kavita" in body
    with Session(engine) as session:
        assert session.exec(select(Enquiry)).one().notified_at is not None


def test_notification_sent_once(client: TestClient, engine: Engine) -> None:
    from app.services.mail_jobs import send_notification

    _contact(client)
    _drain(engine)
    with Session(engine) as session:
        enquiry_id = session.exec(select(Enquiry)).one().id
    assert enquiry_id is not None
    send_notification(engine, get_settings(), enquiry_id)
    assert len(_wait_for_outbox(1)) == 1


# --- error alerts -----------------------------------------------------------------------------


def _error(name: str, msg: str) -> logging.LogRecord:
    return logging.LogRecord(name, logging.ERROR, __file__, 1, msg, None, None)


def test_alerts_once_per_error_type_per_hour() -> None:
    now = {"t": 1000.0}
    handler = AlertHandler(get_settings(), clock=lambda: now["t"], synchronous=True)
    handler.emit(_error("app.request", "unhandled_error"))
    handler.emit(_error("app.request", "unhandled_error"))
    handler.emit(_error("app.jobs", "job_error"))
    alerts = [m for m in mailer.outbox.outbox if "error" in str(m["Subject"])]
    assert len(alerts) == 2
    now["t"] += 3601
    handler.emit(_error("app.request", "unhandled_error"))
    alerts = [m for m in mailer.outbox.outbox if "error" in str(m["Subject"])]
    assert len(alerts) == 3
    assert alerts[0]["To"] == TEAM
    assert "Further errors of this type are not emailed for an hour." in alerts[0].get_content()


def test_alert_body_is_redacted() -> None:
    handler = AlertHandler(get_settings(), synchronous=True)
    handler.emit(_error("app.request", "failed for 9000012345"))
    [alert] = mailer.outbox.outbox
    assert "9000012345" not in alert.get_content()
    assert "******2345" in alert.get_content()


def test_mailer_failures_never_trigger_alerts() -> None:
    handler = AlertHandler(get_settings(), synchronous=True)
    handler.emit(_error("app.services.mailer", "mail_failed"))
    handler.emit(_error("app.services.alerts", "anything"))
    assert mailer.outbox.outbox == []


def test_alert_send_failure_is_swallowed(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*_a: object, **_k: object) -> str:
        raise MailError()

    monkeypatch.setattr("app.services.alerts.send_mail", boom)
    AlertHandler(get_settings(), synchronous=True).emit(_error("app.request", "x"))


def test_memory_transport_is_isolated() -> None:
    transport = MemoryTransport()
    send_mail(get_settings(), to=TEAM, subject="s", body="b", kind="t", transport=transport)
    assert len(transport.outbox) == 1 and mailer.outbox.outbox == []
