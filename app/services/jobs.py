"""Background jobs stored in the database, with a status the owner app can poll.

A worker inside the app process claims queued jobs, runs at most `concurrency` at once, gives
each a timeout, and retries a failed or timed-out job once. Jobs left "running" by a restart are
put back in the queue once at startup (requeue_stale, called by the app before any worker
starts), never by a worker, so one worker starting can never requeue another worker's job.
"""

import asyncio
import logging
import secrets
from collections.abc import Awaitable, Callable
from typing import Any

from sqlalchemy import update
from sqlalchemy.engine import Engine
from sqlmodel import Session, col, select

from app.models import Job, utcnow

logger = logging.getLogger(__name__)

Runner = Callable[[str], Awaitable[int | None]]
# A score job is 3 browser runs (measured 10-16 s locally) plus one Claude call (60 s timeout,
# 2 attempts with backoff). 300 s covers the worst case on a slower server running two jobs.
DEFAULT_TIMEOUT_SECONDS = 300
DEFAULT_CONCURRENCY = 2


def enqueue(
    engine: Engine,
    kind: str,
    business_id: int | None = None,
    payload: dict[str, Any] | None = None,
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
    max_attempts: int = 2,
) -> str:
    """Queue a job and return its unguessable id."""
    job_id = secrets.token_hex(16)
    job = Job(
        id=job_id,
        kind=kind,
        business_id=business_id,
        payload=payload,
        timeout_seconds=timeout_seconds,
        max_attempts=max_attempts,
    )
    with Session(engine) as session:
        session.add(job)
        session.commit()
    logger.info("job_queued", extra={"job_id": job_id, "kind": kind, "business_id": business_id})
    return job_id


def get_job(engine: Engine, job_id: str) -> Job | None:
    """Load one job."""
    with Session(engine) as session:
        return session.get(Job, job_id)


def requeue_stale(engine: Engine) -> int:
    """Put jobs that were running when the app stopped back in the queue."""
    with Session(engine) as session:
        result = session.exec(
            update(Job).where(col(Job.status) == "running").values(status="queued")
        )
        session.commit()
        return int(result.rowcount or 0)


def claim_next(engine: Engine, kinds: set[str]) -> Job | None:
    """Atomically move the oldest queued job of a known kind to running."""
    with Session(engine) as session:
        candidates = session.exec(
            select(Job)
            .where(col(Job.status) == "queued", col(Job.kind).in_(kinds))
            .order_by(col(Job.created_at))
            .limit(5)
        ).all()
        for job in candidates:
            result = session.exec(
                update(Job)
                .where(col(Job.id) == job.id, col(Job.status) == "queued")
                .values(status="running", attempts=Job.attempts + 1, started_at=utcnow())
            )
            session.commit()
            if result.rowcount:
                session.refresh(job)
                session.expunge(job)
                return job
    return None


def _finish(engine: Engine, job_id: str, run_id: int | None) -> None:
    with Session(engine) as session:
        job = session.get(Job, job_id)
        if job is not None:
            job.status, job.run_id, job.error, job.finished_at = "succeeded", run_id, None, utcnow()
            session.add(job)
            session.commit()


def _fail_or_retry(engine: Engine, job_id: str, error: str) -> str:
    with Session(engine) as session:
        job = session.get(Job, job_id)
        if job is None:
            return "missing"
        job.error = error
        if job.attempts < job.max_attempts:
            job.status = "queued"
        else:
            job.status, job.finished_at = "failed", utcnow()
        session.add(job)
        session.commit()
        return job.status


class JobWorker:
    """Runs queued jobs in the background with a concurrency limit, timeouts and one retry."""

    def __init__(
        self,
        engine: Engine,
        runners: dict[str, Runner],
        concurrency: int = DEFAULT_CONCURRENCY,
        poll_seconds: float = 1.0,
    ) -> None:
        self.engine = engine
        self.runners = runners
        self.poll_seconds = poll_seconds
        self._slots = asyncio.Semaphore(concurrency)
        self._tasks: set[asyncio.Task[None]] = set()

    async def run_forever(self) -> None:
        """Keep claiming and running jobs until cancelled."""
        try:
            while True:
                if not await self._start_next():
                    await asyncio.sleep(self.poll_seconds)
        finally:
            for task in self._tasks:
                task.cancel()

    async def run_until_idle(self) -> None:
        """Run jobs until none are queued or running (used by scripts and tests)."""
        while True:
            started = await self._start_next()
            if not started and not self._tasks:
                return
            if not started:
                await asyncio.wait(self._tasks, return_when=asyncio.FIRST_COMPLETED)

    async def _start_next(self) -> bool:
        await self._slots.acquire()
        job = claim_next(self.engine, set(self.runners))
        if job is None:
            self._slots.release()
            return False
        task = asyncio.create_task(self._run(job))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return True

    async def _run(self, job: Job) -> None:
        try:
            run_id = await asyncio.wait_for(self.runners[job.kind](job.id), job.timeout_seconds)
        except TimeoutError:
            outcome = _fail_or_retry(self.engine, job.id, "timeout")
            logger.warning("job_timeout", extra={"job_id": job.id, "now": outcome})
        except Exception as exc:
            outcome = _fail_or_retry(self.engine, job.id, type(exc).__name__)
            logger.exception("job_error", extra={"job_id": job.id, "now": outcome})
        else:
            _finish(self.engine, job.id, run_id)
            logger.info("job_done", extra={"job_id": job.id, "run_id": run_id})
        finally:
            self._slots.release()


TYPICAL_SECONDS = {"score": 45, "intake": 60, "email": 5, "agent": 300, "domains": 30}


def queue_position(engine: Engine, job_id: str) -> tuple[int, int]:
    """(place in line, rough wait in seconds) for a queued job; (0, 0) once it is running."""
    with Session(engine) as session:
        job = session.get(Job, job_id)
        if job is None or job.status != "queued":
            return 0, 0
        ahead = session.exec(
            select(Job).where(
                col(Job.kind) == job.kind,
                col(Job.status).in_(("queued", "running")),
                col(Job.created_at) < job.created_at,
            )
        ).all()
    place = len(ahead) + 1
    rounds = len(ahead) // DEFAULT_CONCURRENCY + 1
    return place, rounds * TYPICAL_SECONDS.get(job.kind, 30)
