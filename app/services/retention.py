"""Data retention, as promised on the privacy page, plus removal of expired sample businesses.

- Customer enquiries are deleted 90 days after they arrive.
- Log files are deleted 30 days after the day they cover.
- Voice notes and price-list photos are deleted after processing (intake); leftovers after a
  day, and those the owner agreed to keep after 30 days.
- Intakes that were never confirmed (transcript, draft details) are deleted after 7 days.
- Businesses whose owner deleted them are wiped once no job for them is running.
- In production, a daily database backup is made here too (7 kept, app/services/backup.py).
"""

import asyncio
import logging
import re
import shutil
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from sqlalchemy import delete, update
from sqlalchemy.engine import Engine
from sqlmodel import Session, col, select

from app.config import Settings
from app.models import ClaudeUsage, Enquiry, IntakeSession, SttUsage
from app.services.backup import daily_backup
from app.services.logs import LOG_FILE
from app.services.publish import purge_deleted, purge_expired_samples

logger = logging.getLogger(__name__)

ENQUIRY_DAYS = 90
LOG_DAYS = 30
INPUT_HOURS = 24
KEPT_INPUT_DAYS = 30
UNCONFIRMED_DAYS = 7
RUN_EVERY_SECONDS = 60 * 60  # hourly, so expired samples go promptly
_ROTATED = re.compile(rf"^{re.escape(LOG_FILE)}\.(\d{{4}}-\d{{2}}-\d{{2}})$")


@dataclass(frozen=True)
class RetentionResult:
    """How much one cleanup run removed."""

    enquiries_deleted: int
    log_files_deleted: int
    inputs_deleted: int = 0
    samples_deleted: int = 0
    kept_inputs_deleted: int = 0
    intakes_deleted: int = 0
    deleted_businesses_wiped: int = 0
    backed_up: bool = False


def purge_enquiries(engine: Engine, now: datetime, days: int = ENQUIRY_DAYS) -> int:
    """Delete enquiries older than `days`. Returns how many were deleted."""
    cutoff = now - timedelta(days=days)
    with Session(engine) as session:
        result = session.exec(delete(Enquiry).where(col(Enquiry.created_at) < cutoff))
        session.commit()
        return int(result.rowcount or 0)


def _log_file_day(path: Path) -> date | None:
    match = _ROTATED.match(path.name)
    return date.fromisoformat(match.group(1)) if match else None


def purge_logs(log_dir: Path, now: datetime, days: int = LOG_DAYS) -> int:
    """Delete rotated log files whose day is more than `days` ago. The current file is kept."""
    if not log_dir.is_dir():
        return 0
    cutoff = (now - timedelta(days=days)).date()
    deleted = 0
    for path in log_dir.iterdir():
        day = _log_file_day(path)
        if day is not None and day < cutoff:
            path.unlink()
            deleted += 1
    return deleted


def purge_stale_inputs(engine: Engine, now: datetime, hours: int = INPUT_HOURS) -> int:
    """Delete voice notes and price-list photos left over for more than a day (unless kept)."""
    cutoff = now - timedelta(hours=hours)
    deleted = 0
    with Session(engine) as session:
        stale = session.exec(
            select(IntakeSession).where(
                col(IntakeSession.input_path).is_not(None),
                col(IntakeSession.keep_input) == False,  # noqa: E712 - SQL expression
                col(IntakeSession.created_at) < cutoff,
            )
        ).all()
        for intake in stale:
            Path(str(intake.input_path)).unlink(missing_ok=True)
            intake.input_path = None
            session.add(intake)
            deleted += 1
        session.commit()
    return deleted


def purge_kept_inputs(engine: Engine, now: datetime, days: int = KEPT_INPUT_DAYS) -> int:
    """Delete voice notes and price-list photos the owner agreed to keep, after `days`."""
    cutoff = now - timedelta(days=days)
    deleted = 0
    with Session(engine) as session:
        kept = session.exec(
            select(IntakeSession).where(
                col(IntakeSession.input_path).is_not(None),
                col(IntakeSession.keep_input) == True,  # noqa: E712 - SQL expression
                col(IntakeSession.created_at) < cutoff,
            )
        ).all()
        for intake in kept:
            Path(str(intake.input_path)).unlink(missing_ok=True)
            intake.input_path = None
            session.add(intake)
            deleted += 1
        session.commit()
    return deleted


def purge_unconfirmed_intakes(
    engine: Engine, uploads_dir: Path, now: datetime, days: int = UNCONFIRMED_DAYS
) -> int:
    """Delete intakes never confirmed as a business after `days`, with their files.

    Usage rows (tokens, audio seconds and cost) stay for cost reporting, without the link.
    """
    cutoff = now - timedelta(days=days)
    with Session(engine) as session:
        old = session.exec(
            select(IntakeSession).where(
                col(IntakeSession.business_id).is_(None),
                col(IntakeSession.created_at) < cutoff,
            )
        ).all()
        ids = [i.id for i in old if i.id is not None]
        if not ids:
            return 0
        for table in (ClaudeUsage, SttUsage):
            session.exec(update(table).where(col(table.intake_id).in_(ids)).values(intake_id=None))
        session.exec(delete(IntakeSession).where(col(IntakeSession.id).in_(ids)))
        session.commit()
    for intake_id in ids:
        shutil.rmtree(uploads_dir / "intake" / str(intake_id), ignore_errors=True)
    return len(ids)


def run_retention(
    engine: Engine, log_dir: Path, now: datetime | None = None, settings: Settings | None = None
) -> RetentionResult:
    """Run every cleanup once."""
    moment = now or datetime.now(UTC)
    samples = purge_expired_samples(engine, settings, moment) if settings else 0
    result = RetentionResult(
        purge_enquiries(engine, moment),
        purge_logs(log_dir, moment),
        purge_stale_inputs(engine, moment),
        samples,
        purge_kept_inputs(engine, moment),
        purge_unconfirmed_intakes(engine, settings.uploads_dir, moment) if settings else 0,
        purge_deleted(engine, settings) if settings else 0,
        bool(settings and settings.is_production and daily_backup(settings.database_url)),
    )
    logger.info("retention_run", extra=asdict(result))
    return result


async def retention_loop(
    engine: Engine,
    log_dir: Path,
    every: float = RUN_EVERY_SECONDS,
    settings: Settings | None = None,
) -> None:
    """Run the cleanup now and then every hour until cancelled. Failures are logged, not raised."""
    while True:
        try:
            await asyncio.to_thread(run_retention, engine, log_dir, None, settings)
        except Exception:
            logger.exception("retention_failed")
        await asyncio.sleep(every)
