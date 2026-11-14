"""SQLite backup and restore.

Backup:  python -m scripts.backup  (writes backups/app-YYYYMMDD-HHMMSS.db, keeps 7 days)
Restore: python -m scripts.backup --restore FILE (stop the app first)
"""

import argparse
import shutil
import sqlite3
import sys
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy.engine import make_url

KEEP_DAYS = 7


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


def main(argv: list[str] | None = None) -> int:
    """Command-line entry point."""
    from app.config import get_settings

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--restore", type=Path, help="backup file to restore")
    parser.add_argument("--dir", type=Path, default=Path("backups"), help="backup directory")
    args = parser.parse_args(argv)
    db_path = sqlite_path(get_settings().database_url)
    if args.restore:
        restore(args.restore, db_path)
        print(f"Restored {args.restore} to {db_path}")
    else:
        print(f"Backup written: {backup(db_path, args.dir)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
