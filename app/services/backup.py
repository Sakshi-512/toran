"""SQLite backup and restore, plus the daily backup run by the app's cleanup loop.

Command line: python -m scripts.backup (see scripts/backup.py).
"""

import logging
import shutil
import sqlite3
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy.engine import make_url

logger = logging.getLogger(__name__)

KEEP_DAYS = 7
BACKUP_DIR = Path("backups")  # /app/backups in Docker, a mounted host folder
BACKUP_EVERY = timedelta(days=1)


def sqlite_path(database_url: str) -> Path:
    """File path of a sqlite:/// database URL."""
    url = make_url(database_url)
    if url.get_backend_name() != "sqlite" or not url.database:
        raise ValueError("Only file-based SQLite databases can be backed up by this script")
    return Path(url.database)


def backup(db_path: Path, backup_dir: Path, now: datetime | None = None) -> Path:
    """Copy the live database safely (SQLite online backup API, WAL-safe) and prune old copies."""
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = (now or datetime.now(UTC)).strftime("%Y%m%d-%H%M%S")
    target = backup_dir / f"app-{stamp}.db"
    with closing(sqlite3.connect(db_path)) as source, closing(sqlite3.connect(target)) as dest:
        source.backup(dest)
    prune(backup_dir, now)
    return target


def prune(backup_dir: Path, now: datetime | None = None) -> list[Path]:
    """Delete backups older than KEEP_DAYS days. Returns the deleted files."""
    cutoff = (now or datetime.now(UTC)) - timedelta(days=KEEP_DAYS)
    removed = []
    for path in backup_dir.glob("app-*.db"):
        try:
            taken = datetime.strptime(path.stem[4:], "%Y%m%d-%H%M%S").replace(tzinfo=UTC)
        except ValueError:
            continue
        if taken < cutoff:
            path.unlink()
            removed.append(path)
    return removed


def restore(backup_file: Path, db_path: Path) -> None:
    """Replace the database with a backup after an integrity check. Stop the app first."""
    with closing(sqlite3.connect(backup_file)) as check:
        if check.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise ValueError(f"{backup_file} failed the integrity check")
    for suffix in ("-wal", "-shm"):
        Path(str(db_path) + suffix).unlink(missing_ok=True)
    shutil.copy2(backup_file, db_path)


def latest_backup(backup_dir: Path) -> datetime | None:
    """When the newest backup in backup_dir was taken."""
    taken = []
    for path in backup_dir.glob("app-*.db"):
        try:
            taken.append(datetime.strptime(path.stem[4:], "%Y%m%d-%H%M%S").replace(tzinfo=UTC))
        except ValueError:
            continue
    return max(taken, default=None)


def daily_backup(
    database_url: str, backup_dir: Path = BACKUP_DIR, now: datetime | None = None
) -> Path | None:
    """Back up once a day (called hourly). Failures are logged as errors, which emails an alert."""
    moment = now or datetime.now(UTC)
    last = latest_backup(backup_dir) if backup_dir.is_dir() else None
    if last is not None and moment - last < BACKUP_EVERY:
        return None
    try:
        path = backup(sqlite_path(database_url), backup_dir, moment)
    except (OSError, sqlite3.Error, ValueError):
        logger.exception("backup_failed")
        return None
    logger.info("backup_done", extra={"file": path.name})
    return path
