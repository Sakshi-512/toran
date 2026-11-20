"""Every agent run, read-only: scores before and after, cost, rounds, and each round's result.

No business names or owner details are printed: a business appears only as its number and
whether it is a sample.

Run: python -m scripts.agent_report (or make agent-report; on the server, make server-agent-report)
"""

import sys

from sqlmodel import Session, col, select

from app.db import get_engine
from app.models import ActionLog, AgentRun, Business


def main() -> int:
    """Print one block per agent run, oldest first."""
    with Session(get_engine()) as session:
        runs = session.exec(select(AgentRun).order_by(col(AgentRun.created_at))).all()
        print(f"Agent runs: {len(runs)}")
        for run in runs:
            business = session.get(Business, run.business_id)
            kind = "deleted" if business is None else ("sample" if business.is_sample else "real")
            before = "-" if run.score_before is None else run.score_before
            after = "-" if run.score_after is None else run.score_after
            print(
                f"\nrun {run.id} · business {run.business_id} ({kind}) · "
                f"{run.created_at:%Y-%m-%d %H:%M} UTC\n"
                f"  status {run.status} · stop {run.stop_reason or '-'} · rounds {run.rounds} · "
                f"score {before} -> {after} · cost ${run.cost_usd_estimate:.4f}"
            )
            results = session.exec(
                select(ActionLog)
                .where(ActionLog.run_id == run.id, ActionLog.step == "round_result")
                .order_by(col(ActionLog.round))
            ).all()
            for log in results:
                detail = log.detail or {}
                outcome = "kept" if detail.get("kept") else "rolled back"
                print(
                    f"  round {log.round}: {detail.get('score_from')} -> {detail.get('score_to')}"
                    f" · {outcome} · AI checks {detail.get('ai_status')}"
                )
    return 0


if __name__ == "__main__":
    sys.exit(main())
