"""SQLite backup and restore from the command line.

Backup:  python -m scripts.backup  (writes backups/app-YYYYMMDD-HHMMSS.db, keeps 7 days)
Restore: python -m scripts.backup --restore FILE (stop the app first)
The app also makes a backup once a day by itself (app/services/backup.py).
"""

import argparse
import sys
from pathlib import Path

from app.services.backup import backup, prune, restore, sqlite_path

__all__ = ["backup", "main", "prune", "restore", "sqlite_path"]


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
