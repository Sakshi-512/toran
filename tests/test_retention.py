import json
import logging
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.engine import Engine
from sqlmodel import Session, select

from app.config import get_settings
from app.models import Enquiry
from app.services.retention import purge_enquiries, purge_logs, run_retention

NOW = datetime(2026, 11, 15, 12, 0, tzinfo=UTC)


def _add_enquiries(engine: Engine, ages_in_days: list[float], now: datetime = NOW) -> None:
    with Session(engine) as session:
        for age in ages_in_days:
            session.add(
                Enquiry(name=f"age {age}", message="hi", created_at=now - timedelta(days=age))
            )
        session.commit()


def _remaining(engine: Engine) -> list[str]:
    with Session(engine) as session:
        return sorted(e.name for e in session.exec(select(Enquiry)))


def test_enquiries_older_than_90_days_deleted(engine: Engine) -> None:
    _add_enquiries(engine, [0, 30, 89.9, 90.1, 120, 400])
    assert purge_enquiries(engine, NOW) == 3
    assert _remaining(engine) == ["age 0", "age 30", "age 89.9"]


def test_enquiry_purge_is_idempotent(engine: Engine) -> None:
    _add_enquiries(engine, [100])
    assert purge_enquiries(engine, NOW) == 1
    assert purge_enquiries(engine, NOW) == 0


def _touch(log_dir: Path, name: str) -> Path:
    path = log_dir / name
    path.write_text("{}\n")
    return path


def test_log_files_older_than_30_days_deleted(tmp_path: Path) -> None:
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    keep = [
        _touch(log_dir, "app.log"),
        _touch(log_dir, "app.log.2026-11-14"),
        _touch(log_dir, "app.log.2026-10-16"),  # exactly 30 days ago
        _touch(log_dir, "notes.txt"),
    ]
    old = [_touch(log_dir, "app.log.2026-10-15"), _touch(log_dir, "app.log.2025-01-01")]
    assert purge_logs(log_dir, NOW) == 2
    assert all(p.exists() for p in keep)
    assert not any(p.exists() for p in old)


def test_missing_log_dir_is_fine(tmp_path: Path) -> None:
    assert purge_logs(tmp_path / "nope", NOW) == 0


def test_run_retention_does_both(engine: Engine, tmp_path: Path) -> None:
    _add_enquiries(engine, [95, 1])
    (tmp_path / "app.log.2026-01-01").write_text("")
    result = run_retention(engine, tmp_path, NOW)
    assert (result.enquiries_deleted, result.log_files_deleted) == (1, 1)


def test_purge_script(engine: Engine, capsys: pytest.CaptureFixture[str]) -> None:
    from scripts.purge import main

    _add_enquiries(engine, [91], now=datetime.now(UTC))
    assert main() == 0
    assert "Deleted 1 enquiries" in capsys.readouterr().out


def test_cleanup_runs_when_app_starts(engine: Engine) -> None:
    _add_enquiries(engine, [91, 2], now=datetime.now(UTC))
    from app.main import create_app

    with TestClient(create_app()):
        deadline = time.monotonic() + 5
        while len(_remaining(engine)) > 1 and time.monotonic() < deadline:
            time.sleep(0.05)
    assert _remaining(engine) == ["age 2"]


def test_logs_written_to_daily_file_and_redacted(client: TestClient) -> None:
    logging.getLogger("test").info("call me on 9876543210")
    log_file = get_settings().log_dir / "app.log"
    lines = [json.loads(line) for line in log_file.read_text().splitlines()]
    message = next(entry for entry in lines if entry["msg"].startswith("call me"))
    assert message["msg"] == "call me on ******3210"
