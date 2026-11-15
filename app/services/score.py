"""Customer Readiness Score (docs/score.md).

Pure functions: measurements in, score items out. Five parts of 20 points. When a check does not
apply, its points are shared among the other checks in the same part, in proportion to their
maximum (pending AI checks included). AI-assessed checks stay "pending" until Claude runs them.
"""

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Literal

from app.services.i18n import SUPPORTED, t

Source = Literal["measured", "ai_assessed"]
Status = Literal["scored", "pending", "not_applicable"]

PARTS: tuple[str, ...] = ("speed", "devices", "language", "trust", "questions")
CONTENT_TIERS_MS: tuple[tuple[int, float], ...] = ((4000, 1.0), (7000, 0.5), (10000, 0.25))
WEIGHT_TIERS_BYTES: tuple[tuple[int, float], ...] = ((500 * 1024, 1.0), (1024 * 1024, 0.5))


@dataclass(frozen=True)
class CheckSpec:
    """One check from docs/score.md."""

    key: str
    part: str
    max_points: int
    source: Source


SPECS: tuple[CheckSpec, ...] = (
    CheckSpec("content_time", "speed", 14, "measured"),
    CheckSpec("page_weight", "speed", 6, "measured"),
    CheckSpec("cta_above_fold", "devices", 8, "measured"),
    CheckSpec("tap_targets", "devices", 4, "measured"),
    CheckSpec("no_sideways_scroll", "devices", 4, "measured"),
    CheckSpec("laptop_no_overflow", "devices", 4, "measured"),
    CheckSpec("language_clarity", "language", 20, "ai_assessed"),
    CheckSpec("phone_visible", "trust", 5, "measured"),
    CheckSpec("area_shown", "trust", 4, "measured"),
    CheckSpec("real_photos", "trust", 4, "ai_assessed"),
    CheckSpec("policy_shown", "trust", 4, "ai_assessed"),
    CheckSpec("https", "trust", 3, "measured"),
    CheckSpec("questions_answered", "questions", 20, "ai_assessed"),
)


@dataclass(frozen=True)
class Outcome:
    """The result of one check before points are shared: a fraction of its points earned."""

    status: Status
    fraction: float = 0.0
    reason_key: str = "score.reason.pending"
    reason_values: dict[str, str] = field(default_factory=dict)
    detail: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Item:
    """A scored item with plain-language reasons in every owner language."""

    key: str
    part: str
    source: Source
    status: Status
    points: float
    max_points: float
    reasons: dict[str, str]
    detail: dict[str, Any]


@dataclass(frozen=True)
class Score:
    """All items plus totals. possible_so_far leaves out pending points."""

    items: tuple[Item, ...]
    total: int
    possible_so_far: float
    pending_points: float

    def part_items(self, part: str) -> list[Item]:
        """Items of one part, in docs/score.md order."""
        return [i for i in self.items if i.part == part]

    def part_points(self, part: str) -> tuple[float, float]:
        """(earned, possible so far) for one part."""
        items = [i for i in self.part_items(part) if i.status == "scored"]
        return sum(i.points for i in items), sum(i.max_points for i in items)


@dataclass(frozen=True)
class Measurements:
    """Median results of the customer tests for one site."""

    content_ms: float
    bytes_transferred: int
    cta_found: bool
    cta_above_fold: bool
    small_targets: int
    sideways_scroll: bool
    laptop_overflow: int
    phone_visible: bool
    area_shown: bool
    address_found: bool
    https: bool
    policy_text_found: bool


@dataclass(frozen=True)
class Context:
    """Facts about the site that decide whether a check applies."""

    is_sample: bool
    is_production: bool


def tier(value: float, tiers: tuple[tuple[int, float], ...], strict: bool = False) -> float:
    """Fraction earned for a value against (limit, fraction) tiers, best first.

    strict=True means the value must be under the limit ("under 500 KB"); otherwise "within".
    """
    for limit, fraction in tiers:
        if value < limit or (not strict and value == limit):
            return fraction
    return 0.0


def _content_time(m: Measurements, _c: Context) -> Outcome:
    seconds = f"{m.content_ms / 1000:.1f}"
    return Outcome(
        "scored",
        tier(m.content_ms, CONTENT_TIERS_MS),
        "score.reason.content_time",
        {"seconds": seconds},
        {"content_ms": round(m.content_ms)},
    )


def _page_weight(m: Measurements, _c: Context) -> Outcome:
    fraction = tier(m.bytes_transferred, WEIGHT_TIERS_BYTES, strict=True)
    return Outcome(
        "scored",
        fraction,
        "score.reason.page_weight",
        {"kb": str(round(m.bytes_transferred / 1024))},
        {"bytes": m.bytes_transferred},
    )


def _cta(m: Measurements, _c: Context) -> Outcome:
    if not m.cta_found:
        return Outcome("scored", 0.0, "score.reason.cta_missing")
    if m.cta_above_fold:
        return Outcome("scored", 1.0, "score.reason.cta_visible")
    return Outcome("scored", 0.0, "score.reason.cta_below_fold")


def _tap_targets(m: Measurements, _c: Context) -> Outcome:
    if m.small_targets == 0:
        return Outcome("scored", 1.0, "score.reason.tap_targets_ok")
    return Outcome(
        "scored",
        0.0,
        "score.reason.tap_targets_small",
        {"count": str(m.small_targets)},
        {"small_targets": m.small_targets},
    )


def _sideways(m: Measurements, _c: Context) -> Outcome:
    if m.sideways_scroll:
        return Outcome("scored", 0.0, "score.reason.sideways_bad")
    return Outcome("scored", 1.0, "score.reason.sideways_ok")


def _laptop(m: Measurements, _c: Context) -> Outcome:
    if m.laptop_overflow == 0:
        return Outcome("scored", 1.0, "score.reason.laptop_ok")
    return Outcome(
        "scored",
        0.0,
        "score.reason.laptop_bad",
        {"count": str(m.laptop_overflow)},
        {"overflow": m.laptop_overflow},
    )


def _phone(m: Measurements, c: Context) -> Outcome:
    if c.is_sample:
        return Outcome("not_applicable", reason_key="score.reason.phone_na")
    if m.phone_visible:
        return Outcome("scored", 1.0, "score.reason.phone_yes")
    return Outcome("scored", 0.0, "score.reason.phone_no")


def _area(m: Measurements, _c: Context) -> Outcome:
    if m.address_found:
        return Outcome("scored", 0.0, "score.reason.area_address")
    if m.area_shown:
        return Outcome("scored", 1.0, "score.reason.area_yes")
    return Outcome("scored", 0.0, "score.reason.area_no")


def _https(m: Measurements, c: Context) -> Outcome:
    if not c.is_production:
        return Outcome("not_applicable", reason_key="score.reason.https_na")
    if m.https:
        return Outcome("scored", 1.0, "score.reason.https_yes")
    return Outcome("scored", 0.0, "score.reason.https_no")


def _pending_policy(m: Measurements, _c: Context) -> Outcome:
    return Outcome("pending", detail={"policy_text_found": m.policy_text_found})


def _pending(_m: Measurements, _c: Context) -> Outcome:
    return Outcome("pending")


CHECKS: dict[str, Callable[[Measurements, Context], Outcome]] = {
    "content_time": _content_time,
    "page_weight": _page_weight,
    "cta_above_fold": _cta,
    "tap_targets": _tap_targets,
    "no_sideways_scroll": _sideways,
    "laptop_no_overflow": _laptop,
    "language_clarity": _pending,
    "phone_visible": _phone,
    "area_shown": _area,
    "real_photos": _pending,
    "policy_shown": _pending_policy,
    "https": _https,
    "questions_answered": _pending,
}


def shared_maximums(statuses: dict[str, Status]) -> dict[str, float]:
    """Each check's maximum after sharing the points of checks that do not apply."""
    maximums: dict[str, float] = {}
    for part in PARTS:
        specs = [s for s in SPECS if s.part == part]
        applicable = [s for s in specs if statuses[s.key] != "not_applicable"]
        spare = sum(s.max_points for s in specs if statuses[s.key] == "not_applicable")
        base = sum(s.max_points for s in applicable)
        for spec in specs:
            if statuses[spec.key] == "not_applicable" or base == 0:
                maximums[spec.key] = 0.0
            else:
                maximums[spec.key] = spec.max_points + spare * spec.max_points / base
    return maximums


def reasons(outcome: Outcome) -> dict[str, str]:
    """The outcome's reason in every owner language."""
    return {lang: t(outcome.reason_key, lang, **outcome.reason_values) for lang in SUPPORTED}


def score(measurements: Measurements, context: Context) -> Score:
    """Score one site from its median measurements."""
    outcomes = {key: check(measurements, context) for key, check in CHECKS.items()}
    maximums = shared_maximums({key: o.status for key, o in outcomes.items()})
    items = []
    for spec in SPECS:
        outcome = outcomes[spec.key]
        maximum = round(maximums[spec.key], 2)
        points = round(outcome.fraction * maximum, 2) if outcome.status == "scored" else 0.0
        items.append(
            Item(
                spec.key,
                spec.part,
                spec.source,
                outcome.status,
                points,
                maximum,
                reasons(outcome),
                outcome.detail,
            )
        )
    scored = [i for i in items if i.status == "scored"]
    pending = [i for i in items if i.status == "pending"]
    return Score(
        items=tuple(items),
        total=round(sum(i.points for i in scored)),
        possible_so_far=round(sum(i.max_points for i in scored), 2),
        pending_points=round(sum(i.max_points for i in pending), 2),
    )
