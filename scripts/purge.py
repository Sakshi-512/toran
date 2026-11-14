"""Run the retention cleanup once: python -m scripts.purge (or make purge)."""

import sys

from app.config import get_settings
from app.db import get_engine
from app.services.retention import run_retention


def main() -> int:
    """Delete enquiries older than 90 days and log files older than 30 days."""
    result = run_retention(get_engine(), get_settings().log_dir)
    print(f"Deleted {result.enquiries_deleted} enquiries and {result.log_files_deleted} log files")
    return 0


if __name__ == "__main__":
    sys.exit(main())
