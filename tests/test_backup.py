from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy.engine import Engine
from sqlmodel import Session, select

from app.models import Business
from scripts.backup import backup, prune, restore, sqlite_path


def test_backup_and_restore_round_trip(env: str, engine: Engine, tmp_path: Path) -> None:
    with Session(engine) as session:
        session.add(Business(slug="before", display_name="Before"))
        session.commit()
    copy = backup(sqlite_path(env), tmp_path / "backups")
    with Session(engine) as session:
        session.add(Business(slug="after", display_name="After"))
        session.commit()
    engine.dispose()
    restore(copy, sqlite_path(env))
    with Session(engine) as session:
        slugs = {b.slug for b in session.exec(select(Business))}
    assert slugs == {"before"}


def test_prune_keeps_seven_days(tmp_path: Path) -> None:
    now = datetime(2026, 11, 14, tzinfo=UTC)
    for days in (1, 6, 8, 30):
        stamp = (now - timedelta(days=days)).strftime("%Y%m%d-%H%M%S")
        (tmp_path / f"app-{stamp}.db").write_bytes(b"")
    removed = prune(tmp_path, now)
    assert len(removed) == 2
    assert len(list(tmp_path.glob("app-*.db"))) == 2
