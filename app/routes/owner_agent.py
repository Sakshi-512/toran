"""Owner pages for the agent fix loop: start it, see how it thinks, undo it, answer its questions.

Approving the changes is the existing publish confirmation (rule 2).
"""

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse
from sqlmodel import Session
from starlette.responses import Response

from app.config import get_settings
from app.db import get_engine
from app.models import AgentRun, Business
from app.services import owner_questions as questions
from app.services.agent import start_agent, undo_changes
from app.services.agent_report import agent_view
from app.services.auth import require_owner
from app.services.errors import AppError
from app.services.i18n import pick_language
from app.services.security import verify_csrf
from app.templating import render

router = APIRouter(prefix="/owner")
START_ERRORS = {"agent.busy": "busy", "agent.daily_limit": "limit", "publish.no_draft": "draft"}


@router.post("/agent", include_in_schema=False, dependencies=[Depends(verify_csrf)])
def start(business_id: int = Depends(require_owner)) -> RedirectResponse:
    """Her "Let Toran improve my draft" button."""
    try:
        start_agent(get_engine(), get_settings(), business_id)
    except AppError as exc:
        code = START_ERRORS.get(exc.key)
        if code is None:
            raise
        return RedirectResponse(f"/owner?agent_error={code}#agent", status_code=303)
    return RedirectResponse("/owner#agent", status_code=303)


def _own_run(session: Session, business_id: int, run_id: str) -> AgentRun:
    run = session.get(AgentRun, run_id)
    if run is None or run.business_id != business_id:
        raise AppError("error.not_found", status_code=404)
    return run


@router.get("/agent/{run_id}", include_in_schema=False)
def panel(run_id: str, request: Request, business_id: int = Depends(require_owner)) -> Response:
    """The "how it thinks" panel: before and after first, then each round."""
    with Session(get_engine()) as session:
        run = _own_run(session, business_id, run_id)
        business = session.get(Business, business_id)
        view = agent_view(session, run, pick_language(request))
    return render(request, "app/owner_agent.html", {"business": business, "agent": view})


@router.post("/agent/{run_id}/undo", include_in_schema=False, dependencies=[Depends(verify_csrf)])
def undo(run_id: str, business_id: int = Depends(require_owner)) -> RedirectResponse:
    """Her "Undo these changes"."""
    undo_changes(get_engine(), get_settings(), business_id, run_id)
    return RedirectResponse("/owner#site", status_code=303)


@router.post(
    "/questions/{question_id}", include_in_schema=False, dependencies=[Depends(verify_csrf)]
)
async def answer(
    question_id: int, request: Request, business_id: int = Depends(require_owner)
) -> Response:
    """Check her answer and show it back to her exactly as it will appear, to confirm."""
    form = await request.form()
    raw = str(form.get("answer", ""))
    with Session(get_engine()) as session:
        question = questions.own_question(session, business_id, question_id)
        business = session.get(Business, business_id)
    context = {"business": business, "question": question, "answer": raw}
    try:
        context["cleaned"] = questions.clean_answer(question.topic, raw)
    except AppError as exc:
        return render(request, "app/owner_question.html", {**context, "error_key": exc.key}, 422)
    return render(request, "app/owner_question.html", context)


@router.post(
    "/questions/{question_id}/confirm",
    include_in_schema=False,
    dependencies=[Depends(verify_csrf)],
)
async def confirm(
    question_id: int, request: Request, business_id: int = Depends(require_owner)
) -> Response:
    """She confirmed: add it to her profile, rebuild and re-test the draft."""
    form = await request.form()
    questions.apply_answer(
        get_engine(), get_settings(), business_id, question_id, str(form.get("answer", ""))
    )
    return RedirectResponse("/owner#site", status_code=303)


@router.post(
    "/questions/{question_id}/dismiss",
    include_in_schema=False,
    dependencies=[Depends(verify_csrf)],
)
def dismiss(question_id: int, business_id: int = Depends(require_owner)) -> RedirectResponse:
    """She chose not to answer."""
    questions.dismiss(get_engine(), business_id, question_id)
    return RedirectResponse("/owner#questions", status_code=303)
