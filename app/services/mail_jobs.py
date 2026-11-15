"""Notification emails as background jobs.

A job carries only an enquiry id; the email is built from the database when the job runs, so
the message text never sits in the job queue and nothing is lost if the app restarts.
"""

import asyncio
import logging
from datetime import UTC
from zoneinfo import ZoneInfo

from sqlalchemy.engine import Engine
from sqlmodel import Session

from app.config import Settings
from app.models import Business, Enquiry, Job, utcnow
from app.services.jobs import Runner, enqueue
from app.services.mailer import send_mail, valid_email
from app.services.sites import site_origin

logger = logging.getLogger(__name__)

EMAIL_JOB = "email"
EMAIL_TIMEOUT_SECONDS = 60
IST = ZoneInfo("Asia/Kolkata")


def queue_notification(engine: Engine, enquiry_id: int) -> str:
    """Queue the notification email for one saved enquiry or contact message."""
    return enqueue(
        engine,
        EMAIL_JOB,
        payload={"enquiry_id": enquiry_id},
        timeout_seconds=EMAIL_TIMEOUT_SECONDS,
        max_attempts=1,
    )


def _received(enquiry: Enquiry) -> str:
    created = (
        enquiry.created_at if enquiry.created_at.tzinfo else enquiry.created_at.replace(tzinfo=UTC)
    )
    return created.astimezone(IST).strftime("%d/%m/%Y %H:%M IST")


def contact_email(settings: Settings, enquiry: Enquiry) -> tuple[str, str, str | None]:
    """(subject, body, reply_to) for a main-site contact form message."""
    lines = [f"New message from the {settings.app_name} contact form.", "", f"Name: {enquiry.name}"]
    if enquiry.email:
        lines.append(f"Email: {enquiry.email}")
    if enquiry.phone:
        lines.append(f"Phone: {enquiry.phone}")
    lines += [f"Received: {_received(enquiry)}", "", "Message:", enquiry.message]
    reply_to = enquiry.email if enquiry.email and valid_email(enquiry.email) else None
    return f"{settings.app_name} contact form: {enquiry.name}", "\n".join(lines), reply_to


def enquiry_email(settings: Settings, enquiry: Enquiry, business: Business) -> tuple[str, str]:
    """(subject, body) for a customer enquiry sent from a business site."""
    sample = "[Sample] " if business.is_sample else ""
    note = " (sample business)" if business.is_sample else ""
    lines = [
        f"New enquiry for {business.display_name}{note}.",
        f"Site: {site_origin(business.slug, settings)}/",
        "",
        f"Customer: {enquiry.name}",
        f"Phone: {enquiry.phone or 'not given'}",
        f"Received: {_received(enquiry)}",
        "",
        "Message:",
        enquiry.message,
    ]
    return f"{sample}New enquiry for {business.display_name}", "\n".join(lines)


def send_notification(engine: Engine, settings: Settings, enquiry_id: int) -> None:
    """Email one enquiry or contact message to CONTACT_TO_EMAIL and mark it notified."""
    if not settings.contact_to_email:
        logger.warning("mail_skipped_no_recipient", extra={"enquiry_id": enquiry_id})
        return
    with Session(engine) as session:
        enquiry = session.get(Enquiry, enquiry_id)
        if enquiry is None or enquiry.notified_at is not None:
            return
        reply_to: str | None = None
        if enquiry.business_id is None:
            subject, body, reply_to = contact_email(settings, enquiry)
            kind = "contact"
        else:
            business = session.get(Business, enquiry.business_id)
            if business is None:
                return
            subject, body = enquiry_email(settings, enquiry, business)
            kind = "enquiry"
        send_mail(
            settings,
            to=settings.contact_to_email,
            subject=subject,
            body=body,
            kind=kind,
            reply_to=reply_to,
        )
        enquiry.notified_at = utcnow()
        session.add(enquiry)
        session.commit()


def email_runner(engine: Engine, settings: Settings) -> Runner:
    """Job runner for notification emails (SMTP runs in a thread so the loop stays free)."""

    async def runner(job_id: str) -> None:
        with Session(engine) as session:
            job = session.get(Job, job_id)
            enquiry_id = int((job.payload or {}).get("enquiry_id", 0)) if job else 0
        await asyncio.to_thread(send_notification, engine, settings, enquiry_id)

    return runner
