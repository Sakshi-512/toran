"""Run one agent fix loop on a business's draft: python -m scripts.agent_run <slug> [url]."""

import asyncio
import sys

from sqlmodel import Session, col, select

from app.config import get_settings
from app.db import get_engine
from app.models import ActionLog, AgentRun, Business
from app.services.agent import AGENT_JOB, agent_runner, start_agent
from app.services.agent_report import agent_view
from app.services.jobs import JobWorker


def main(argv: list[str]) -> int:
    """Start a loop, run it here, and print each round, the final score and the cost."""
    if not argv:
        print("usage: python -m scripts.agent_run <slug> [draft url]")
        return 2
    settings, engine = get_settings(), get_engine()
    with Session(engine) as session:
        business = session.exec(select(Business).where(Business.slug == argv[0])).first()
    if business is None or business.id is None:
        print(f"No business with slug {argv[0]}")
        return 1
    run_id = start_agent(engine, settings, business.id, argv[1] if len(argv) > 1 else None)
    asyncio.run(JobWorker(engine, {AGENT_JOB: agent_runner(engine, settings)}).run_until_idle())
    with Session(engine) as session:
        run = session.get(AgentRun, run_id)
        assert run is not None  # noqa: S101
        view = agent_view(session, run, "en")
        rejected = session.exec(
            select(ActionLog).where(
                ActionLog.run_id == run_id, ActionLog.step == "tool", col(ActionLog.ok).is_(False)
            )
        ).all()
    print(f"Agent run {run_id}: {view.before} -> {view.after} ({run.stop_reason})")
    for r in view.rounds:
        print(f"\nRound {r.number}: {r.score_from} -> {r.score_to} kept={r.kept}")
        for wrong in r.wrong:
            print(f"  wrong: {wrong}")
        for tool in r.tools:
            mark = "ok " if tool.ok else "NO "
            print(f"  {mark}{tool.tool} {tool.input_text.replace(chr(10), '')}")
            if not tool.ok:
                print(f"      {tool.result}")
    print(f"\nMain fixes: {view.fixes}")
    print(f"Rejected calls: {len(rejected)}. Estimated cost: ${run.cost_usd_estimate:.4f}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
