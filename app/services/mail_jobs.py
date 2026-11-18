"""Notification emails as background jobs.

A job carries only an enquiry id; the email is built from the database when the job runs, so
the message text never sits in the job queue and nothing is lost if the app restarts.
"""

import asyncio
import logging
from datetime import UTC
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy.engine import Engine
from sqlmodel import Session

from app.config import Settings
from app.models import Business, DomainRequest, Enquiry, Job, utcnow
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


def queue_team_email(engine: Engine, payload: dict[str, Any]) -> str:
    """Queue a note to the team mailbox: {"team": "domain_request", "request_id": n} or
    {"team": "deletion", "at": iso time}. The text is built when the job runs."""
    return enqueue(
        engine, EMAIL_JOB, payload=payload, timeout_seconds=EMAIL_TIMEOUT_SECONDS, max_attempts=2
    )


def team_email(
    engine: Engine, settings: Settings, payload: dict[str, Any]
) -> tuple[str, str] | None:
    """(subject, body) for a team note, or None if there is nothing to send."""
    kind = payload.get("team")
    if kind == "deletion":
        when = str(payload.get("at", ""))
        return (
            f"{settings.app_name}: a business deleted its site and data",
            "A business owner deleted their website and all their data from the owner page.\n"
            f"Time (UTC): {when}\n"
            "No personal details are included in this message.\n",
        )
    if kind == "domain_request":
        with Session(engine) as session:
            request = session.get(DomainRequest, int(payload.get("request_id", 0)))
            business = session.get(Business, request.business_id) if request else None
            if request is None or business is None or request.notified_at is not None:
                return None
            approved = request.approved_at
            approved = approved if approved.tzinfo else approved.replace(tzinfo=UTC)
            body = (
                f"{business.display_name} asked for a web address in the owner app.\n\n"
                f"Domain: {request.domain}\n"
                f"First-year price shown (before tax): {request.price_usd:.2f} {request.currency}\n"
                f"The owner approved it: {approved.astimezone(IST):%d/%m/%Y %H:%M} IST\n"
                f"The owner's site: {site_origin(business.slug, settings)}/\n\n"
                "To do: buy it on godaddy.com with the hackathon credit, point its DNS at the "
                "server (gddy CLI), then tell the owner it is ready. "
                "Nothing was bought by the app.\n"
            )
            domain = request.domain
            request.notified_at = utcnow()
            session.add(request)
            session.commit()
        return f"{settings.app_name}: web address request {domain}", body
    return None


def send_team_email(engine: Engine, settings: Settings, payload: dict[str, Any]) -> None:
    """Send one team note to CONTACT_TO_EMAIL."""
    if not settings.contact_to_email:
        logger.warning("mail_skipped_no_recipient", extra={"team": payload.get("team")})
        return
    message = team_email(engine, settings, payload)
    if message is None:
        return
    subject, body = message
    send_mail(settings, to=settings.contact_to_email, subject=subject, body=body, kind="team")


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
            payload = dict(job.payload or {}) if job else {}
        if "team" in payload:
            await asyncio.to_thread(send_team_email, engine, settings, payload)
            return
        enquiry_id = int(payload.get("enquiry_id", 0))
        await asyncio.to_thread(send_notification, engine, settings, enquiry_id)

    return runner
