"""The "how it thinks" panel, built from the action log, in the owner's language.

First the before and after (score and the main fixes), then each round: what was wrong, what
the agent did and why (one short sentence from our own templates, so it is always in the owner
language), which tool it used and how the score changed. A detailed view for judges adds
Claude's own reason, the exact tool input, rejected calls, tokens and cost.
"""

import json
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

from sqlmodel import Session, col, select

from app.models import ActionLog, AgentRun, Job, OwnerQuestion
from app.services.agent import MAX_ROUNDS
from app.services.i18n import queue_text, t
from app.services.jobs import queue_position


@dataclass
class ToolView:
    """One tool call, for the owner and for the detailed view."""

    tool: str
    ok: bool
    did: str
    why: str
    claude_why: str
    input: dict[str, Any]
    result: str
    change: dict[str, Any] = field(default_factory=dict)

    @property
    def input_text(self) -> str:
        """The exact tool input, readable (Devanagari kept as is)."""
        return json.dumps(self.input, ensure_ascii=False, indent=1)


@dataclass
class RoundView:
    """One round of the loop."""

    number: int
    wrong: list[str] = field(default_factory=list)
    tools: list[ToolView] = field(default_factory=list)
    score_from: int | None = None
    score_to: int | None = None
    kept: bool | None = None
    thinking: list[str] = field(default_factory=list)
    tokens_in: int = 0
    tokens_out: int = 0
    cost_usd: float = 0.0

    @property
    def accepted(self) -> list[ToolView]:
        """Calls that passed the checks, without the bookkeeping tools."""
        return [v for v in self.tools if v.ok and v.tool not in ("run_tests", "rebuild_draft")]

    @property
    def rejected(self) -> list[ToolView]:
        """Calls the code checks refused."""
        return [v for v in self.tools if not v.ok]


@dataclass
class AgentView:
    """Everything the panel and the owner card show for one agent run."""

    run: AgentRun
    state: str  # queued, running, done or failed
    progress: str
    before: int | None
    after: int | None
    fixes: list[str]
    stop_text: str
    rounds: list[RoundView]
    cost_usd: float

    @property
    def improved(self) -> bool:
        """True if the draft now has kept changes that score higher."""
        return bool(self.fixes) and (self.after or 0) > (self.before or 0)

    @property
    def awaiting_approval(self) -> bool:
        """Kept changes the owner has neither published nor undone."""
        return (
            self.state == "done"
            and self.improved
            and self.run.approved_at is None
            and self.run.discarded_at is None
        )


def _lang_name(code: str, lang: str) -> str:
    return t(f"agent.language.{code}", lang)


def describe(change: dict[str, Any], lang: str) -> tuple[str, str]:
    """(what the agent did, why) for one accepted change, in the owner's language."""
    kind = change.get("kind")
    if kind == "translation":
        language = _lang_name(str(change.get("lang")), lang)
        did = t(
            f"agent.did.translation.{change.get('field')}",
            lang,
            language=language,
            source=change.get("source", ""),
            text=change.get("text", ""),
        )
        return did, t("agent.why.translation", lang, language=language)
    if kind == "layout":
        key = str(change.get("change"))
        return t(f"agent.did.layout.{key}", lang), t(f"agent.why.layout.{key}", lang)
    if kind == "images":
        return t("agent.did.images", lang), t("agent.why.images", lang)
    if kind == "question":
        topic = str(change.get("topic"))
        return t(f"agent.did.question.{topic}", lang), t("agent.why.question", lang)
    return "", ""


def _fixes(rounds: list[RoundView], lang: str) -> list[str]:
    """The main fixes kept in the draft, grouped so the summary stays short."""
    translations: Counter[str] = Counter()
    others: list[str] = []
    for r in rounds:
        if not r.kept:
            continue
        for tool in r.accepted:
            change = tool.change
            if change.get("kind") == "translation":
                translations[str(change.get("lang"))] += 1
            elif change.get("kind") != "question" and tool.did not in others:
                others.append(tool.did)
    grouped = [
        t(
            "agent.fix.translations_one" if n == 1 else "agent.fix.translations",
            lang,
            language=_lang_name(code, lang),
            n=n,
        )
        for code, n in sorted(translations.items())
    ]
    return grouped + others


def _rounds(rows: list[ActionLog], lang: str) -> list[RoundView]:
    by_number: dict[int, RoundView] = {}
    for row in rows:
        if row.round == 0:
            continue
        view = by_number.setdefault(row.round, RoundView(row.round))
        detail = row.detail or {}
        if row.step == "diagnosis":
            for lost in detail.get("lost", []):
                reason = (lost.get("reason") or {}).get(lang, "")
                name = t(f"score.check.{lost.get('key')}", lang)
                view.wrong.append(f"{name}: {reason}" if reason else name)
        elif row.step == "tool":
            change = detail.get("change") or {}
            did, why = describe(change, lang) if row.ok else ("", "")
            view.tools.append(
                ToolView(
                    tool=row.tool or "",
                    ok=row.ok,
                    did=did,
                    why=why,
                    claude_why=row.decision or "",
                    input=dict(detail.get("input") or {}),
                    result=str(detail.get("result", "")),
                    change=change,
                )
            )
        elif row.step == "round_result":
            view.score_from = detail.get("score_from")
            view.score_to = detail.get("score_to")
            view.kept = bool(detail.get("kept"))
        elif row.step == "thinking" and row.decision:
            view.thinking.append(row.decision)
        elif row.step == "claude_call":
            view.tokens_in += int(detail.get("input_tokens", 0)) + int(
                detail.get("cache_read_tokens", 0)
            )
            view.tokens_out += int(detail.get("output_tokens", 0))
            view.cost_usd += float(detail.get("cost_usd", 0.0))
    return [by_number[n] for n in sorted(by_number)]


def _state(session: Session, run: AgentRun) -> str:
    if run.status in ("queued", "running") and run.job_id:
        job = session.get(Job, run.job_id)
        if job is not None and job.status == "failed":
            return "failed"
    return run.status


def agent_view(session: Session, run: AgentRun, lang: str) -> AgentView:
    """The panel for one agent run."""
    rows = list(
        session.exec(
            select(ActionLog).where(ActionLog.run_id == run.id).order_by(col(ActionLog.id))
        ).all()
    )
    rounds = _rounds(rows, lang)
    state = _state(session, run)
    if state == "queued":
        progress = t("agent.queued", lang)
        place, wait = queue_position(session.get_bind(), run.job_id) if run.job_id else (0, 0)  # type: ignore[arg-type]
        if place > 1:  # more than one run is ahead: say where the owner is in line
            progress = f"{progress} {queue_text(place, wait, lang)}"
    elif state == "running":
        current = max([r.number for r in rounds], default=0) or 1
        progress = t("agent.running", lang, round=current, max=MAX_ROUNDS)
    else:
        progress = ""
    stop_text = t(f"agent.stop.{run.stop_reason}", lang) if run.stop_reason else ""
    if state == "failed":
        stop_text = t("agent.failed", lang)
    return AgentView(
        run=run,
        state=state,
        progress=progress,
        before=run.score_before,
        after=run.score_after,
        fixes=_fixes(rounds, lang),
        stop_text=stop_text,
        rounds=rounds,
        cost_usd=run.cost_usd_estimate,
    )


def open_questions(session: Session, business_id: int) -> list[OwnerQuestion]:
    """Questions from the agent still waiting for the owner, oldest first."""
    return list(
        session.exec(
            select(OwnerQuestion)
            .where(OwnerQuestion.business_id == business_id, OwnerQuestion.status == "open")
            .order_by(col(OwnerQuestion.id))
        ).all()
    )


def pending_changes(session: Session, business_id: int, lang: str) -> list[str]:
    """Fixes in the draft the owner has not approved yet (shown on the publish confirmation)."""
    runs = session.exec(
        select(AgentRun)
        .where(
            AgentRun.business_id == business_id,
            AgentRun.status == "done",
            col(AgentRun.approved_at).is_(None),
            col(AgentRun.discarded_at).is_(None),
        )
        .order_by(col(AgentRun.created_at))
    ).all()
    fixes: list[str] = []
    for run in runs:
        for fix in agent_view(session, run, lang).fixes:
            if fix not in fixes:
                fixes.append(fix)
    return fixes


def agent_context(session: Session, business_id: int, lang: str, has_draft: bool) -> dict[str, Any]:
    """The agent card and the owner's open questions, for the owner page."""
    from app.services.agent import latest_run

    run = latest_run(session, business_id)
    view = agent_view(session, run, lang) if run else None
    busy = view is not None and view.state in ("queued", "running")
    return {
        "agent": view,
        "questions": open_questions(session, business_id),
        "can_improve": has_draft and not busy,
    }
