"""What the public home page shows: honest numbers, a real before and after, and the gallery.

Every number comes from real data and is left out (never a placeholder) until it is measured.
Anything produced in mock mode is excluded: a Claude call counts only if it was a real call.
"""

import io
import logging
import statistics
from dataclasses import dataclass
from datetime import UTC, date, datetime
from functools import lru_cache
from pathlib import Path
from zoneinfo import ZoneInfo

from PIL import Image
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator
from sqlalchemy import exists
from sqlmodel import Session, col, select

from app.config import Settings
from app.models import AgentRun, Business, ClaudeUsage, IntakeSession, TestRun
from app.services.accuracy import accuracy
from app.services.address_guard import looks_like_address
from app.services.i18n import SUPPORTED
from app.services.publish import latest_profile

MIN_REAL_FOR_ACCURACY = 2
IST = ZoneInfo("Asia/Kolkata")
THUMB_WIDTH = 240
THUMB_HEIGHT = 400
THUMB_QUALITY = 70
# The latest real eval run, copied by hand from evals/reports/ (see evals/README.md).
EVAL_SUMMARY = Path(__file__).resolve().parents[2] / "evals" / "latest.json"

logger = logging.getLogger(__name__)


class EvalSummary(BaseModel):
    """Totals of the latest real eval run on the reviewed test cases (not live traffic)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    checks_passed: int = Field(ge=0)
    checks_total: int = Field(gt=0)
    cases: int = Field(gt=0)
    prices_exact: bool
    run_date: date

    @model_validator(mode="after")
    def _passed_within_total(self) -> "EvalSummary":
        if self.checks_passed > self.checks_total:
            raise ValueError("checks_passed is more than checks_total")
        return self


def load_eval_summary(path: Path | None = None) -> EvalSummary | None:
    """The eval summary, or None if the file is missing or not valid (the figure is hidden)."""
    path = path or EVAL_SUMMARY
    try:
        return EvalSummary.model_validate_json(path.read_bytes())
    except FileNotFoundError:
        return None
    except (OSError, ValidationError, ValueError) as error:
        logger.warning("eval_summary_invalid", extra={"error_type": type(error).__name__})
        return None


@dataclass(frozen=True)
class Stats:
    """Measured numbers; None means not measured yet (the page shows nothing)."""

    check_cost_usd: float | None = None
    check_count: int = 0
    minutes_to_tested_draft: float | None = None
    draft_count: int = 0
    field_accuracy: float | None = None
    price_accuracy: float | None = None
    accuracy_count: int = 0
    # The day of the newest measurement behind each figure (shown next to it).
    check_date: date | None = None
    draft_date: date | None = None
    accuracy_date: date | None = None
    # Facts and test results, shown in their own group (not live measurements).
    languages: int = len(SUPPORTED)
    eval: EvalSummary | None = None

    @property
    def any(self) -> bool:
        """True if at least one live number is measured."""
        return any(
            v is not None
            for v in (self.check_cost_usd, self.minutes_to_tested_draft, self.field_accuracy)
        )

    @property
    def facts_any(self) -> bool:
        """True if the facts and test results group has something to show."""
        return self.languages > 0 or self.eval is not None


def _real_intake_ids(session: Session) -> set[int]:
    """Intakes whose extraction was a real (not mock) Claude call."""
    rows = session.exec(
        select(ClaudeUsage.intake_id).where(
            ClaudeUsage.purpose == "extract",
            col(ClaudeUsage.is_mock).is_(False),
            col(ClaudeUsage.intake_id).is_not(None),
        )
    ).all()
    return {r for r in rows if r is not None}


def home_stats(session: Session) -> Stats:
    """Cost of one full check, voice note to tested draft, and accuracy on real voice notes."""
    checks = session.exec(
        select(ClaudeUsage.cost_usd_estimate, ClaudeUsage.created_at).where(
            ClaudeUsage.purpose == "ai_checks", col(ClaudeUsage.is_mock).is_(False)
        )
    ).all()
    costs = [cost for cost, _ in checks]
    real = _real_intake_ids(session)
    intakes = (
        session.exec(select(IntakeSession).where(col(IntakeSession.id).in_(real))).all()
        if real
        else []
    )
    timed = [i for i in intakes if i.seconds_to_draft_scored]
    seconds = [i.seconds_to_draft_scored for i in timed if i.seconds_to_draft_scored]
    voice = [
        i
        for i in intakes
        if i.source == "voice" and i.status == "done" and i.first_guess and i.draft
    ]
    real_voice = [i for i in voice if _is_real_business(session, i.business_id)]
    results = [accuracy(i.first_guess or {}, i.draft or {}) for i in real_voice]
    enough = len(results) >= MIN_REAL_FOR_ACCURACY
    return Stats(
        check_cost_usd=statistics.median(costs) if costs else None,
        check_count=len(costs),
        minutes_to_tested_draft=statistics.median(seconds) / 60 if seconds else None,
        draft_count=len(seconds),
        field_accuracy=statistics.mean(r.field_accuracy for r in results) if enough else None,
        price_accuracy=statistics.mean(r.price_accuracy for r in results) if enough else None,
        accuracy_count=len(results) if enough else 0,
        check_date=_newest([when for _, when in checks]),
        draft_date=_newest([i.created_at for i in timed]),
        accuracy_date=_newest([i.created_at for i in real_voice]) if enough else None,
        eval=load_eval_summary(),
    )


def _newest(moments: list[datetime]) -> date | None:
    """The calendar day (India time) of the newest moment."""
    if not moments:
        return None
    newest = max(m if m.tzinfo else m.replace(tzinfo=UTC) for m in moments)
    return newest.astimezone(IST).date()


def _is_real_business(session: Session, business_id: int | None) -> bool:
    business = session.get(Business, business_id) if business_id else None
    return business is not None and not business.is_sample and business.deleted_at is None


# --- before and after ---


def is_demo(business: Business | None) -> bool:
    """A seeded demo business: all of its data is sample data, safe to show publicly."""
    return (
        business is not None
        and business.is_sample
        and business.source == "seed"
        and business.deleted_at is None
    )


def made_with_real_claude(session: Session, run: AgentRun) -> bool:
    """True if the agent run's decisions were real Claude calls (never mock numbers)."""
    statement = select(ClaudeUsage.id).where(
        ClaudeUsage.business_id == run.business_id,
        ClaudeUsage.purpose == "agent",
        col(ClaudeUsage.is_mock).is_(False),
        col(ClaudeUsage.created_at) >= run.created_at,
    )
    if run.finished_at is not None:
        statement = statement.where(col(ClaudeUsage.created_at) <= run.finished_at)
    return session.exec(select(exists(statement))).one()


def public_agent_run(session: Session, run_id: str) -> AgentRun | None:
    """An agent run anyone may see: finished, on a demo business, with real Claude calls."""
    run = session.get(AgentRun, run_id)
    if run is None or run.status != "done":
        return None
    if not is_demo(session.get(Business, run.business_id)):
        return None
    return run if made_with_real_claude(session, run) else None


def latest_demo_run(session: Session) -> AgentRun | None:
    """The newest real agent run on a demo business that raised the score."""
    runs = session.exec(
        select(AgentRun)
        .where(AgentRun.status == "done")
        .order_by(col(AgentRun.finished_at).desc())
        .limit(20)
    ).all()
    for run in runs:
        if (run.score_after or 0) <= (run.score_before or 0):
            continue
        if public_agent_run(session, run.id) is not None:
            return run
    return None


# --- gallery ---


@dataclass(frozen=True)
class GalleryItem:
    """One business card in the gallery."""

    slug: str
    name: str
    area: str
    score: int | None
    has_thumb: bool


def in_gallery(business: Business) -> bool:
    """Real businesses need to be live and to have said yes; demo businesses need to be live."""
    if business.deleted_at is not None or business.status != "live":
        return False
    if business.is_sample:
        return is_demo(business)
    return business.gallery_opt_in and business.consent_given_at is not None


def _latest_live_run(session: Session, business_id: int) -> TestRun | None:
    return session.exec(
        select(TestRun)
        .where(TestRun.business_id == business_id, TestRun.site_kind == "live")
        .order_by(col(TestRun.id).desc())
    ).first()


def gallery(session: Session) -> tuple[list[GalleryItem], list[GalleryItem]]:
    """(real businesses, demo businesses), each sorted by score."""
    real: list[GalleryItem] = []
    demo: list[GalleryItem] = []
    for business in session.exec(select(Business).where(Business.status == "live")).all():
        if business.id is None or not in_gallery(business):
            continue
        version = latest_profile(session, business.id)
        area = str((version.profile if version else {}).get("area", ""))
        run = _latest_live_run(session, business.id)
        item = GalleryItem(
            slug=business.slug,
            name=business.display_name,
            area="" if looks_like_address(area) else area,
            score=run.total if run and not run.pending_points else None,
            has_thumb=bool(run and run.screenshot_path),
        )
        (demo if business.is_sample else real).append(item)
    for items in (real, demo):
        items.sort(key=lambda i: (-(i.score or 0), i.name))
    return real, demo


def thumbnail(session: Session, settings: Settings, slug: str) -> bytes | None:
    """A small WebP of the site's latest live phone screenshot, only for gallery businesses."""
    business = session.exec(select(Business).where(Business.slug == slug)).first()
    if business is None or business.id is None or not in_gallery(business):
        return None
    run = _latest_live_run(session, business.id)
    if run is None or not run.screenshot_path:
        return None
    path = settings.runs_dir / run.screenshot_path
    if not path.is_file():
        return None
    return _thumb(path, path.stat().st_mtime)


@lru_cache(maxsize=64)
def _thumb(path: Path, _mtime: float) -> bytes:
    with Image.open(path) as opened:
        image = opened.convert("RGB")
        top = image.crop((0, 0, image.width, min(image.height, round(image.width * 5 / 3))))
        top.thumbnail((THUMB_WIDTH, THUMB_HEIGHT))
        out = io.BytesIO()
        top.save(out, format="WEBP", quality=THUMB_QUALITY, method=4)
    return out.getvalue()
