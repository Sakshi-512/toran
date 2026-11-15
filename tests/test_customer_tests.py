"""Real Chromium against real local sites. Marked "browser"."""

import asyncio
from collections.abc import Callable
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.engine import Engine
from sqlmodel import Session, select

from app.config import get_settings
from app.models import Business, ScoreItem, TestRun
from app.services.customer_tests import Expectations, median_measurements, run_customer_tests
from app.services.jobs import JobWorker, enqueue, get_job
from app.services.samples import get_sample
from app.services.score import Context, score
from app.services.score_runs import SCORE_JOB, expectations, score_runner
from app.services.sites import site_dir
from tests.live_server import live_server

pytestmark = pytest.mark.browser
SLUG = "asha-home-bakery"
SAMPLE_CONTEXT = Context(is_sample=True, is_production=False)


def _sample_expectations() -> Expectations:
    sample = get_sample(SLUG)
    assert sample is not None
    return expectations(sample.profile)


def test_real_checks_on_sample_site_and_stable_scores(
    seed_sample: Callable[[str], Business],
) -> None:
    seed_sample(SLUG)
    with live_server() as port:
        url = f"http://{SLUG}.localhost:{port}/"
        first = asyncio.run(run_customer_tests(url, _sample_expectations()))
        second = asyncio.run(run_customer_tests(url, _sample_expectations()))
    m = median_measurements(first)
    assert m.cta_found and m.cta_above_fold
    assert m.small_targets == 0, first[0].small_examples
    assert not m.sideways_scroll
    assert m.laptop_overflow == 0
    assert m.area_shown and not m.address_found
    assert not m.phone_visible  # sample sites never show a number
    assert not m.https
    assert 100_000 < m.bytes_transferred < 500 * 1024
    assert 0 < m.content_ms < 10_000
    assert first[1].screenshot.startswith(b"\x89PNG")

    a = score(m, SAMPLE_CONTEXT)
    b = score(median_measurements(second), SAMPLE_CONTEXT)
    assert abs(a.total - b.total) <= 2
    assert [i.status for i in a.items] == [i.status for i in b.items]


def _write_broken_site(slug: str) -> None:
    root = site_dir(slug, get_settings())
    (root / "assets").mkdir(parents=True, exist_ok=True)
    (root / "assets" / "bad.css").write_text(
        ".wide { width: 2000px; height: 20px; background: #ccc; }"
        " .tiny { display: inline-block; width: 20px; height: 20px; }"
        " .spacer { height: 6000px; }"
    )
    (root / "index.html").write_text(
        '<!doctype html><html lang="en"><head><meta name="viewport" '
        'content="width=device-width, initial-scale=1"><link rel="stylesheet" '
        'href="assets/bad.css"></head><body><h1>Broken</h1><a class="tiny" href="#">x</a>'
        '<div class="wide"></div><div class="spacer"></div>'
        '<div class="cta"><a class="btn btn-main" href="#">Order</a></div>'
        "<p>Flat 12, Shanti Society</p><p>Refund policy: none</p></body></html>"
    )


def test_checks_catch_problems(seed_sample: Callable[[str], Business]) -> None:
    seed_sample(SLUG)
    _write_broken_site(SLUG)
    with live_server() as port:
        runs = asyncio.run(
            run_customer_tests(
                f"http://{SLUG}.localhost:{port}/",
                Expectations("919876543210", ("Kothrud",)),
                runs=1,
            )
        )
    m = median_measurements(runs)
    assert m.cta_found and not m.cta_above_fold
    assert m.small_targets >= 1
    assert m.sideways_scroll
    assert m.laptop_overflow >= 1
    assert not m.area_shown
    assert m.policy_text_found


def test_score_job_end_to_end(
    seed_sample: Callable[[str], Business], engine: Engine, client: TestClient, tmp_path: Path
) -> None:
    business = seed_sample(SLUG)
    settings = get_settings()
    with live_server() as port:
        job_id = enqueue(
            engine, SCORE_JOB, business.id, {"url": f"http://{SLUG}.localhost:{port}/"}
        )
        asyncio.run(JobWorker(engine, {SCORE_JOB: score_runner(engine, settings)}).run_until_idle())
    job = get_job(engine, job_id)
    assert job is not None and job.status == "succeeded", job and job.error
    with Session(engine) as session:
        run = session.get(TestRun, job.run_id)
        assert run is not None
        items = session.exec(select(ScoreItem).where(ScoreItem.run_id == run.id)).all()
    assert len(items) == 13
    assert all(i.status != "pending" for i in items)  # AI checks filled in (mock mode)
    assert run.pending_points == 0 and round(run.possible_so_far) == 100
    assert run.ai_status == "done" and run.ai_model == "claude-sonnet-5-5"
    assert run.ai_cost_usd_estimate > 0
    assert run.profile_version_id is not None
    assert len(run.site_hash) == 64
    assert run.duration_seconds < 300
    assert (settings.runs_dir / str(run.screenshot_path)).is_file()
    assert all(i.reason_en and i.reason_hi and i.reason_mr for i in items)

    report = client.get(f"/dev/score/run/{run.id}")
    assert report.status_code == 200
    assert f"{run.total} of {round(run.possible_so_far)} possible so far" in report.text
    assert "AI-assessed" in report.text and "Measured" in report.text
    assert "Not applicable: sample businesses have no real phone number." in report.text
    assert "AI checks answered by claude-sonnet-5-5, estimated cost $" in report.text
    assert "of 100 possible so far" in report.text
    shot = client.get(f"/dev/score/run/{run.id}/screenshot.png")
    assert shot.status_code == 200 and shot.content.startswith(b"\x89PNG")
    status = client.get(f"/api/jobs/{job_id}").json()
    assert status == {"status": "succeeded", "attempts": 1, "run_id": run.id, "message": ""}
