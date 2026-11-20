"""The read-only reports for the server: accuracy (with edited field names, never values) and
agent runs (scores, cost, kept or rolled back per round), with no names or owner details."""

import pytest
from sqlalchemy.engine import Engine
from sqlmodel import Session

from app.models import ActionLog, AgentRun, Business, IntakeSession
from scripts import accuracy_report, agent_report
from tests.test_publish import _business

PHONE = "9876543210"


def test_accuracy_report_shows_fractions_and_field_names_only(
    engine: Engine, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    business_id, _, _ = _business(engine, slug="real-parlour")
    guess = {
        "business_name": "Sunita Parlor",
        "area": "Kothrud",
        "products": [
            {"name": "Facial", "unit": "service", "options": [{"label": "", "price": 500}]}
        ],
    }
    final = {
        "business_name": "Sunita Beauty Parlour",
        "area": "Kothrud",
        "phone": PHONE,
        "products": [
            {"name": "Facial", "unit": "service", "options": [{"label": "", "price": 600}]}
        ],
    }
    with Session(engine) as session:
        session.add(
            IntakeSession(
                source="voice",
                business_id=business_id,
                status="done",
                first_guess=guess,
                draft=final,
            )
        )
        session.commit()
    monkeypatch.setattr("sys.argv", ["accuracy_report", "--fields"])
    accuracy_report.main()
    out = capsys.readouterr().out
    assert f"business {business_id} (voice): fields 2/5 (40%), prices 0/1 (0%)" in out
    assert "edited: business_name (set -> set)" in out
    assert "edited: phone (empty -> set)" in out
    assert "edited: product 1.option 1.price (set -> set)" in out
    for private in (PHONE, "Sunita", "Facial", "Kothrud", "600"):
        assert private not in out


def test_agent_report_prints_runs_and_rounds_without_names(
    engine: Engine, capsys: pytest.CaptureFixture[str]
) -> None:
    business_id, _, _ = _business(engine, slug="real-bakery")
    with Session(engine) as session:
        name = session.get(Business, business_id).display_name  # type: ignore[union-attr]
        session.add(
            AgentRun(
                id="ag_report",
                business_id=business_id,
                status="done",
                stop_reason="no_improvement",
                rounds=2,
                score_before=80,
                score_after=84,
                cost_usd_estimate=0.1234,
            )
        )
        for number, (start, end, kept) in enumerate([(80, 84, True), (84, 83, False)], 1):
            session.add(
                ActionLog(
                    business_id=business_id,
                    run_id="ag_report",
                    round=number,
                    step="round_result",
                    detail={
                        "score_from": start,
                        "score_to": end,
                        "kept": kept,
                        "ai_status": "done",
                    },
                )
            )
        session.commit()
    agent_report.main()
    out = capsys.readouterr().out
    assert f"business {business_id} (real)" in out
    assert "score 80 -> 84 · cost $0.1234" in out and "rounds 2" in out
    assert "round 1: 80 -> 84 · kept" in out and "round 2: 84 -> 83 · rolled back" in out
    assert name not in out
