"""End to end, with real Chromium: sample voice note -> confirm -> draft -> score -> publish ->
live site -> undo. Claude and speech-to-text are in mock mode."""

import asyncio
import re

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.engine import Engine
from sqlmodel import Session, select

from app.config import get_settings
from app.models import Business, IntakeSession, Job, TestRun
from app.services.intake import INTAKE_JOB, intake_runner
from app.services.jobs import JobWorker
from app.services.score_runs import SCORE_JOB, score_runner
from app.services.security import CSRF_FIELD
from tests.live_server import live_server
from tests.test_publish import publish_form

pytestmark = pytest.mark.browser


def _csrf(client: TestClient, path: str) -> str:
    return re.search(rf'name="{CSRF_FIELD}" value="([^"]+)"', client.get(path).text).group(1)  # type: ignore[union-attr]


def _run(engine: Engine, kind: str) -> None:
    runners = {
        INTAKE_JOB: intake_runner(engine, get_settings()),
        SCORE_JOB: score_runner(engine, get_settings()),
    }
    asyncio.run(JobWorker(engine, {kind: runners[kind]}).run_until_idle())


def _point_jobs_at(engine: Engine, url: str) -> None:
    """Score jobs normally use port 8000; in the test the app runs on a free port."""
    with Session(engine) as session:
        for job in session.exec(select(Job).where(Job.kind == SCORE_JOB, Job.status == "queued")):
            job.payload = {**(job.payload or {}), "url": url}
            session.add(job)
        session.commit()


def test_voice_note_to_live_site_and_undo(client: TestClient, engine: Engine) -> None:
    # 1. A judge tries the Marathi sample voice note.
    started = client.post(
        "/start",
        data={CSRF_FIELD: _csrf(client, "/start"), "sample": "sample-mr"},
        follow_redirects=False,
    )
    token = started.headers["location"].split("/")[2]
    _run(engine, INTAKE_JOB)
    # 2. The owner confirms the owner's details (adds the owner's phone number, which the voice note
    # lacked).
    page = client.get(f"/start/{token}/confirm").text
    fields = {
        k: v
        for k, v in re.findall(r'name="([^"]+)" value="([^"]*)"', page)
        if not k.endswith("_remove") and k not in ("languages", "order_methods", "action")
    }
    fields.update(
        phone="9000012345",
        business_type="bakery",
        action="confirm",
        order_methods="whatsapp",
        languages="mr",
    )
    done = client.post(f"/start/{token}/confirm", data=fields)
    assert done.status_code == 200
    link = re.search(r'href="http://localhost:8000(/o/[^"]+)"', done.text).group(1)  # type: ignore[union-attr]
    pin = re.search(r'class="secret-value pin">(\d{4})<', done.text).group(1)  # type: ignore[union-attr]
    with Session(engine) as session:
        business = session.exec(select(Business)).one()
    slug = business.slug
    assert business.is_sample and business.status == "draft"
    with live_server() as port:
        # 3. The draft is tested on the internal address and scored.
        _point_jobs_at(engine, f"http://127.0.0.1:{port}/internal/draft/{slug}/")
        _run(engine, SCORE_JOB)
        # 4. The owner signs in, sees the score and publishes.
        client.post(link, data={CSRF_FIELD: _csrf(client, link), "pin": pin})
        home = client.get("/owner").text
        assert "status-draft" in home and "Make my site live" in home
        client.post("/owner/publish", data=publish_form(client))
        # 5. The live site is tested at <slug>.localhost.
        _point_jobs_at(engine, f"http://{slug}.localhost:{port}/")
        _run(engine, SCORE_JOB)
    with Session(engine) as session:
        runs = session.exec(select(TestRun).order_by(TestRun.id)).all()  # type: ignore[arg-type]
        intake = session.exec(select(IntakeSession)).one()
    assert [r.site_kind for r in runs] == ["draft", "live"]
    assert all(r.total > 0 and r.possible_so_far == pytest.approx(100) for r in runs)
    assert runs[1].site_version_id is not None
    assert intake.seconds_to_draft_scored is not None and intake.seconds_to_live is not None
    assert intake.seconds_to_draft_scored <= intake.seconds_to_live
    live = client.get("/", headers={"Host": f"{slug}.localhost:8000"})
    assert live.status_code == 200 and "sample-badge" in live.text
    home = client.get("/owner").text
    assert "status-live" in home and home.count("/owner/score/") >= 2
    # 6. Undo the first publish: back to draft, and the site is no longer public.
    client.post("/owner/undo", data={CSRF_FIELD: _csrf(client, "/owner")})
    assert client.get("/", headers={"Host": f"{slug}.localhost:8000"}).status_code == 404
    assert "status-draft" in client.get("/owner").text
