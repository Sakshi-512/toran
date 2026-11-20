"""Answers to the agent's questions (ask_owner).

The owner's answer is checked, shown back to the owner exactly as it will appear, and only added to
the owner's
profile after the owner confirms (a new profile version, so undo still works). Then the draft is
rebuilt and tested again. The owner's answer is data: it only ever fills one profile field.
"""

import re

from pydantic import ValidationError
from sqlalchemy.engine import Engine
from sqlmodel import Session, col, select

from app.config import Settings
from app.models import OwnerQuestion, ProfileVersion, utcnow
from app.schemas import BusinessProfile
from app.services import publish as pub
from app.services.address_guard import address_reason
from app.services.errors import AppError
from app.services.score_runs import queue_score

MAX_AREA = 60
_SPLIT = re.compile(r"[,\n;]+")
TEXT_TOPICS = ("policy", "lead_time", "delivery_areas")
# Longest answer allowed for each topic (the forms use the same numbers as their maxlength).
ANSWER_LIMITS = {"policy": 500, "lead_time": 120, "delivery_areas": 600}


def own_question(session: Session, business_id: int, question_id: int) -> OwnerQuestion:
    """An open question that belongs to this business, else a friendly not-found."""
    question = session.get(OwnerQuestion, question_id)
    if question is None or question.business_id != business_id or question.status != "open":
        raise AppError("error.not_found", status_code=404)
    return question


def clean_answer(topic: str, raw: str) -> str:
    """The owner's answer, tidied and checked for this topic. Raises a friendly error if it is not
    ok."""
    text = " ".join((raw or "").split()) if topic != "delivery_areas" else (raw or "").strip()
    if topic not in TEXT_TOPICS or not text:
        raise AppError("agent.answer_empty", status_code=422)
    if len(text) > ANSWER_LIMITS[topic]:
        raise AppError("agent.answer_long", status_code=422)
    parts = _areas(text) if topic == "delivery_areas" else [text]
    if any(address_reason(part) for part in parts):
        raise AppError("agent.answer_address", status_code=422)
    return ", ".join(parts) if topic == "delivery_areas" else text


def _areas(text: str) -> list[str]:
    areas = [a.strip() for a in _SPLIT.split(text) if a.strip()]
    if not areas or any(len(a) > MAX_AREA for a in areas) or len(areas) > 30:
        raise AppError("agent.answer_long", status_code=422)
    return areas


def apply_answer(
    engine: Engine, settings: Settings, business_id: int, question_id: int, raw: str
) -> None:
    """The owner confirmed: save the owner's answer as a new profile version, rebuild and re-test
    the draft."""
    with Session(engine) as session:
        question = own_question(session, business_id, question_id)
        answer = clean_answer(question.topic, raw)
        latest = pub.latest_profile(session, business_id)
        if latest is None:
            raise AppError("error.not_found", status_code=404)
        data = dict(latest.profile)
        if question.topic == "delivery_areas":
            data["delivery_areas"] = [*data.get("delivery_areas", []), *answer.split(", ")]
        else:
            data[question.topic] = answer
        try:
            profile = BusinessProfile.model_validate(data)
        except ValidationError as exc:
            raise AppError("agent.answer_long", status_code=422) from exc
        session.add(
            ProfileVersion(
                business_id=business_id,
                version=latest.version + 1,
                profile=profile.model_dump(),
                created_by="owner",
                note=f"answered agent question: {question.topic}",
            )
        )
        question.status, question.answer, question.answered_at = "confirmed", answer, utcnow()
        session.add(question)
        session.commit()
    pub.build_draft(engine, settings, business_id)
    queue_score(engine, business_id, "draft")


def dismiss(engine: Engine, business_id: int, question_id: int) -> None:
    """The owner chose not to answer (or added photos themselves)."""
    with Session(engine) as session:
        question = own_question(session, business_id, question_id)
        question.status, question.answered_at = "dismissed", utcnow()
        session.add(question)
        session.commit()


def dismiss_topic(engine: Engine, business_id: int, topic: str) -> None:
    """Close open questions on a topic the owner has just dealt with (for example the owner added
    photos)."""
    with Session(engine) as session:
        for question in session.exec(
            select(OwnerQuestion).where(
                OwnerQuestion.business_id == business_id,
                OwnerQuestion.topic == topic,
                col(OwnerQuestion.status) == "open",
            )
        ).all():
            question.status, question.answered_at = "answered", utcnow()
            session.add(question)
        session.commit()
