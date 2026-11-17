"""Regression tests for the independent code review (docs/review.md), one section per finding."""

import asyncio
import io
import re
import sqlite3
import time
from collections.abc import Iterator
from datetime import timedelta
from fnmatch import fnmatch
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy.engine import Engine
from sqlmodel import Session, select

from app.config import get_settings
from app.models import AgentRun, Business, DomainLookup, Job, Photo, utcnow
from app.services import bodylimit, godaddy
from app.services import publish as pub
from app.services.agent import AGENT_JOB, agent_runner, start_agent
from app.services.claude_client import ClaudeClient, Reply
from app.services.errors import AppError
from app.services.jobs import JobWorker, claim_next, enqueue, get_job
from app.services.security import CSRF_FIELD
from tests.conftest import ROOT
from tests.test_agent import RUN, TRANSLATE, Script, _home_foods, scores  # noqa: F401
from tests.test_owner_polish import _csrf, _sign_in
from tests.test_publish import _business

# --- 1: every file the app reads at run time is in the Docker image ---


def _copied_sources() -> list[str]:
    sources: list[str] = []
    for line in (ROOT / "Dockerfile").read_text().splitlines():
        if line.startswith("COPY "):
            parts = [p for p in line.split()[1:] if not p.startswith("--")]
            sources += [p.rstrip("/") for p in parts[:-1]]
    return sources


def _ignored(relative: str) -> bool:
    ignored = False
    for raw in (ROOT / ".dockerignore").read_text().splitlines():
        rule = raw.strip()
        if not rule or rule.startswith("#"):
            continue
        negate, pattern = rule.startswith("!"), rule.lstrip("!").rstrip("/")
        if relative == pattern or relative.startswith(pattern + "/") or fnmatch(relative, pattern):
            ignored = not negate
    return ignored


def in_image(path: Path) -> bool:
    """True if the Dockerfile copies this file and .dockerignore does not exclude it."""
    relative = path.resolve().relative_to(ROOT).as_posix()
    copied = any(relative == s or relative.startswith(s + "/") for s in _copied_sources())
    return copied and not _ignored(relative)


def test_in_image_helper_reads_the_build_files() -> None:
    assert in_image(ROOT / "app" / "main.py")
    assert in_image(ROOT / "tests" / "fixtures" / "profiles" / "asha-home-bakery.json")
    assert not in_image(ROOT / "tests" / "conftest.py")


def test_sample_voice_notes_ship_in_the_image(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.services.stt import STT_SAMPLES, MockSTT

    read: list[Path] = []
    original = Path.read_text

    def recording(self: Path, *args: Any, **kwargs: Any) -> str:
        read.append(self)
        return original(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", recording)
    for sample in STT_SAMPLES:
        MockSTT().sample(sample)
    monkeypatch.undo()
    assert read and all(in_image(path) for path in read), read


# --- 2: a deleted business's session never opens another business ---


def test_old_session_cannot_open_a_new_business_after_delete(
    client: TestClient, engine: Engine
) -> None:
    old_id, _ = _sign_in(client, engine)
    assert client.get("/owner").status_code == 200
    pub.delete_business_data(engine, get_settings(), old_id)
    new_id, _, _ = _business(engine, slug="someone-else")
    assert new_id != old_id  # ids are never reused
    assert client.get("/owner", follow_redirects=False).status_code == 401


def test_session_from_before_new_access_is_refused(client: TestClient, engine: Engine) -> None:
    from app.services.auth import DEVICE_COOKIE, issue_access

    business_id, _ = _sign_in(client, engine)
    client.cookies.delete(DEVICE_COOKIE)  # only the session cookie is under test here
    with Session(engine) as session:
        business = session.get(Business, business_id)
        assert business is not None
        issue_access(business)  # a new link and PIN signs out every old session
        session.add(business)
        session.commit()
    assert client.get("/owner", follow_redirects=False).status_code == 401


def test_business_ids_use_autoincrement(engine: Engine) -> None:
    with engine.connect() as connection:
        sql = connection.exec_driver_sql(
            "SELECT sql FROM sqlite_master WHERE name = 'business'"
        ).scalar()
    assert "AUTOINCREMENT" in str(sql).upper()


def test_autoincrement_migration_keeps_existing_data(tmp_path: Path) -> None:
    from alembic import command
    from alembic.config import Config

    url = f"sqlite:///{tmp_path / 'old.db'}"
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.attributes["database_url"] = url
    cfg.attributes["configure_logger"] = False
    command.upgrade(cfg, "0012")
    with sqlite3.connect(tmp_path / "old.db") as db:
        db.execute(
            "INSERT INTO business (id, slug, display_name, business_type, theme, owner_language,"
            " status, is_sample, source, gallery_opt_in, pin_failures, created_at, updated_at)"
            " VALUES (7, 'kept', 'Kept', 'bakery', 'warm', 'en', 'live', 0, 'intake', 0, 0,"
            " '2026-11-01', '2026-11-01')"
        )
        db.execute(
            "INSERT INTO profileversion (business_id, version, profile, created_by, created_at)"
            " VALUES (7, 1, '{}', 'owner', '2026-11-01')"
        )
    command.upgrade(cfg, "head")
    with sqlite3.connect(tmp_path / "old.db") as db:
        assert db.execute("SELECT slug FROM business WHERE id = 7").fetchone() == ("kept",)
        assert db.execute("SELECT session_epoch FROM business").fetchone()[0]
        assert db.execute("SELECT COUNT(*) FROM profileversion").fetchone() == (1,)
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []


# --- 3: the agent loop never blocks the web server while it waits for Claude ---


class SlowScript(Script):
    """Scripted Claude that takes a while to answer, like the real API."""

    def send(self, request: dict[str, Any], fixture_key: str) -> Reply:
        time.sleep(0.6)
        return super().send(request, fixture_key)


def test_agent_does_not_block_the_event_loop(engine: Engine, scores: list[int]) -> None:  # noqa: F811
    business_id = _home_foods(engine)
    scores += [80, 92]
    client = ClaudeClient(get_settings(), engine, transport=SlowScript([[TRANSLATE, RUN]]))
    start_agent(engine, get_settings(), business_id)

    async def main() -> float:
        gaps: list[float] = []
        done = asyncio.Event()

        async def ticker() -> None:
            last = time.monotonic()
            while not done.is_set():
                await asyncio.sleep(0.02)
                now = time.monotonic()
                gaps.append(now - last)
                last = now

        task = asyncio.create_task(ticker())
        worker = JobWorker(engine, {AGENT_JOB: agent_runner(engine, get_settings(), client)})
        await worker.run_until_idle()
        done.set()
        await task
        return max(gaps)

    assert asyncio.run(main()) < 0.3


# --- 4: request bodies are limited before they are read ---


@pytest.fixture
def small_limit(monkeypatch: pytest.MonkeyPatch) -> Iterator[int]:
    monkeypatch.setattr(bodylimit, "MAX_REQUEST_BYTES", 1000)
    yield 1000


def test_declared_oversized_body_is_refused(client: TestClient, small_limit: int) -> None:
    response = client.post(
        "/start",
        content=b"x" * (small_limit + 1),
        headers={"content-type": "multipart/form-data; boundary=x"},
    )
    assert response.status_code == 413
    assert "too large" in response.text


def test_streamed_oversized_body_is_refused(client: TestClient, small_limit: int) -> None:
    def chunks() -> Iterator[bytes]:
        for _ in range(5):
            yield b"x" * 400

    response = client.post(
        "/start", content=chunks(), headers={"content-type": "multipart/form-data; boundary=x"}
    )
    assert response.status_code == 413


def _png() -> bytes:
    out = io.BytesIO()
    Image.new("RGB", (8, 8), "red").save(out, format="PNG")
    return out.getvalue()


def test_too_many_files_in_one_form_are_refused(client: TestClient, engine: Engine) -> None:
    business_id, _ = _sign_in(client, engine)
    files = [("photos", (f"p{i}.png", _png(), "image/png")) for i in range(bodylimit.MAX_FILES + 1)]
    response = client.post("/owner/photos", data={CSRF_FIELD: _csrf(client, "/owner")}, files=files)
    assert response.status_code == 400
    with Session(engine) as session:
        assert session.exec(select(Photo).where(Photo.business_id == business_id)).all() == []


# --- 5: nothing goes live while the agent is changing the draft, or after it changed ---


def test_publish_refused_while_the_agent_works(engine: Engine) -> None:
    business_id = _home_foods(engine)
    start_agent(engine, get_settings(), business_id)
    with pytest.raises(AppError) as caught:
        pub.publish(engine, get_settings(), business_id)
    assert caught.value.key == "publish.agent_busy"
    with Session(engine) as session:
        assert session.get(Business, business_id).status == "draft"  # type: ignore[union-attr]


def test_publish_refused_when_the_draft_changed_since_she_saw_it(
    client: TestClient, engine: Engine
) -> None:
    business_id, _ = _sign_in(client, engine)
    pub.build_draft(engine, get_settings(), business_id)
    page = client.get("/owner/publish").text
    shown = re.search(r'name="draft_hash" value="([0-9a-f]{64})"', page)
    assert shown is not None
    pub.build_draft(engine, get_settings(), business_id, theme="simple")  # changed after
    response = client.post(
        "/owner/publish",
        data={CSRF_FIELD: _csrf(client, "/owner/publish"), "draft_hash": shown.group(1)},
    )
    assert response.status_code == 409
    with Session(engine) as session:
        assert session.get(Business, business_id).status == "draft"  # type: ignore[union-attr]


# --- 6: a crashed or timed-out agent run or lookup never blocks her for good ---


def test_crashed_agent_run_is_closed_and_she_can_try_again(
    engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.services import agent as agent_svc

    business_id = _home_foods(engine)

    async def broken(*_args: Any, **_kwargs: Any) -> int:
        raise RuntimeError("browser crashed")

    monkeypatch.setattr(agent_svc, "score_business", broken)
    run_id = start_agent(engine, get_settings(), business_id)
    asyncio.run(
        JobWorker(engine, {AGENT_JOB: agent_runner(engine, get_settings())}).run_until_idle()
    )
    with Session(engine) as session:
        run = session.get(AgentRun, run_id)
        assert run is not None and run.status == "done" and run.stop_reason == "error"
    start_agent(engine, get_settings(), business_id)  # not "busy"


def test_agent_run_whose_job_failed_does_not_block(engine: Engine) -> None:
    business_id = _home_foods(engine)
    run_id = start_agent(engine, get_settings(), business_id)
    with Session(engine) as session:
        run = session.get(AgentRun, run_id)
        assert run is not None and run.job_id is not None
        run.status = "running"
        job = session.get(Job, run.job_id)
        assert job is not None
        job.status = "failed"  # timed out twice
        session.add_all([run, job])
        session.commit()
    start_agent(engine, get_settings(), business_id)
    with Session(engine) as session:
        assert session.get(AgentRun, run_id).status == "done"  # type: ignore[union-attr]


def test_stuck_domain_lookup_does_not_block(engine: Engine) -> None:
    business_id, _, _ = _business(engine)
    with Session(engine) as session:
        stuck = DomainLookup(
            business_id=business_id,
            status="running",
            created_at=utcnow() - timedelta(seconds=godaddy.JOB_TIMEOUT_SECONDS * 2),
        )
        session.add(stuck)
        session.commit()
        stuck_id = stuck.id
    assert godaddy.start_lookup(engine, business_id) != stuck_id
    with Session(engine) as session:
        assert session.get(DomainLookup, stuck_id).status == "unavailable"  # type: ignore[union-attr]


# --- 7: a starting worker never puts another worker's running job back in the queue ---


def test_starting_worker_leaves_running_jobs_alone(engine: Engine) -> None:
    job_id = enqueue(engine, "email")
    assert claim_next(engine, {"email"}) is not None  # the email worker is running it

    async def start_other_worker() -> None:
        async def runner(_job_id: str) -> None:
            return None

        task = asyncio.create_task(JobWorker(engine, {"score": runner}).run_forever())
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(start_other_worker())
    job = get_job(engine, job_id)
    assert job is not None and job.status == "running"
