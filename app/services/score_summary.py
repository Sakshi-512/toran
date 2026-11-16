"""Plain score wording for owners: "88/100" and one sentence, or "a few checks are still running".

The detailed report (parts, points, measured/AI labels, cost) stays in the dev report.
"""

from dataclasses import dataclass, field

from app.models import ScoreItem, TestRun
from app.services.i18n import t

READY = 85
NEARLY = 60


@dataclass
class OwnerSummary:
    """What an owner sees for one run."""

    complete: bool
    total: int
    to_improve: list[ScoreItem] = field(default_factory=list)
    already_good: list[ScoreItem] = field(default_factory=list)

    def headline(self, lang: str) -> str:
        """ "88/100", or that a few checks are still running."""
        return f"{self.total}/100" if self.complete else t("owner.checks_running", lang)

    def sentence(self, lang: str) -> str:
        """One plain sentence about readiness and the number of things to improve."""
        if not self.complete:
            return ""
        if self.total >= READY:
            first = t("owner.ready_most", lang)
        elif self.total >= NEARLY:
            first = t("owner.nearly_ready", lang)
        else:
            first = t("owner.needs_work", lang)
        count = len(self.to_improve)
        if count == 0:
            second = t("owner.improve_none", lang)
        elif count == 1:
            second = t("owner.improve_one", lang)
        else:
            second = t("owner.improve_many", lang, n=count)
        return f"{first} {second}"


def owner_summary(run: TestRun, items: list[ScoreItem]) -> OwnerSummary:
    """Summarise a run: complete only when no AI check is still pending."""
    scored = [i for i in items if i.status == "scored"]
    return OwnerSummary(
        complete=run.pending_points == 0 and not any(i.status == "pending" for i in items),
        total=run.total,
        to_improve=[i for i in scored if i.points < i.max_points - 0.01],
        already_good=[i for i in scored if i.points >= i.max_points - 0.01],
    )
