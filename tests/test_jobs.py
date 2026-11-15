import asyncio
import time

from fastapi.testclient import TestClient
from sqlalchemy.engine import Engine
from sqlmodel import Session

from app.models import Job
from app.services.customer_tests import chromium_slot, peak_browsers, reset_peak
from app.services.jobs import JobWorker, enqueue, get_job, requeue_stale


def _run(engine: Engine, runners: dict, concurrency: int = 2) -> None:  # type: ignore[type-arg]
    asyncio.run(JobWorker(engine, runners, concurrency=concurrency).run_until_idle())


def test_successful_job(engine: Engine) -> None:
    async def runner(job_id: str) -> int:
        return 42

    job_id = enqueue(engine, "demo")
    _run(engine, {"demo": runner})
    job = get_job(engine, job_id)
    assert job is not None
    assert (job.status, job.attempts, job.run_id, job.error) == ("succeeded", 1, 42, None)
    assert job.finished_at is not None


def test_timeout_is_retried_once_then_fails(engine: Engine) -> None:
    calls: list[str] = []

    async def stuck(job_id: str) -> int:
        calls.append(job_id)
        await asyncio.sleep(30)
        return 1

    job_id = enqueue(engine, "demo", timeout_seconds=1)
    started = time.monotonic()
    _run(engine, {"demo": stuck})
    job = get_job(engine, job_id)
    assert job is not None
    assert (job.status, job.attempts, job.error) == ("failed", 2, "timeout")
    assert len(calls) == 2
    assert time.monotonic() - started < 5


def test_failure_then_success_on_retry(engine: Engine) -> None:
    attempts = {"n": 0}

    async def flaky(job_id: str) -> int:
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise ConnectionError("browser crashed")
        return 7

    job_id = enqueue(engine, "demo")
    _run(engine, {"demo": flaky})
    job = get_job(engine, job_id)
    assert job is not None
    assert (job.status, job.attempts, job.run_id) == ("succeeded", 2, 7)


def test_two_failures_mark_job_failed(engine: Engine) -> None:
    async def broken(job_id: str) -> int:
        raise ValueError("bad site")

    job_id = enqueue(engine, "demo")
    _run(engine, {"demo": broken})
    job = get_job(engine, job_id)
    assert job is not None
    assert (job.status, job.attempts, job.error) == ("failed", 2, "ValueError")


def test_worker_runs_at_most_two_jobs_at_once(engine: Engine) -> None:
    state = {"now": 0, "peak": 0}

    async def runner(job_id: str) -> int:
        state["now"] += 1
        state["peak"] = max(state["peak"], state["now"])
        await asyncio.sleep(0.05)
        state["now"] -= 1
        return 1

    ids = [enqueue(engine, "demo") for _ in range(5)]
    _run(engine, {"demo": runner})
    assert state["peak"] == 2
    assert all(get_job(engine, i).status == "succeeded" for i in ids)  # type: ignore[union-attr]


def test_chromium_slots_never_exceed_two() -> None:
    async def main() -> None:
        reset_peak()

        async def use_browser() -> None:
            async with chromium_slot():
                await asyncio.sleep(0.05)

        await asyncio.gather(*(use_browser() for _ in range(5)))

    asyncio.run(main())
    assert peak_browsers() == 2


def test_unknown_kinds_are_left_alone(engine: Engine) -> None:
    job_id = enqueue(engine, "other")
    _run(engine, {})
    assert get_job(engine, job_id).status == "queued"  # type: ignore[union-attr]


def test_stale_running_jobs_are_requeued(engine: Engine) -> None:
    job_id = enqueue(engine, "demo")
    with Session(engine) as session:
        job = session.get(Job, job_id)
        assert job is not None
        job.status = "running"
        session.add(job)
        session.commit()
    assert requeue_stale(engine) == 1
    assert get_job(engine, job_id).status == "queued"  # type: ignore[union-attr]


def test_job_status_endpoint(client: TestClient, engine: Engine) -> None:
    job_id = enqueue(engine, "other")
    response = client.get(f"/api/jobs/{job_id}", headers={"Accept-Language": "hi"})
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "queued"
    assert body["message"] == "शुरू होने का इंतज़ार"
    assert client.get("/api/jobs/nope", headers={"Accept": "application/json"}).status_code == 404
