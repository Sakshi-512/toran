"""Accuracy from real use: Claude's first guess vs what owners confirmed (real businesses only).

Run: python -m scripts.accuracy_report (or make accuracy)
"""

import statistics
import sys

from sqlmodel import Session, col, select

from app.db import get_engine
from app.models import Business, IntakeSession
from app.services.accuracy import accuracy, correction_rate


def _median_minutes(label: str, seconds: list[int | None]) -> None:
    values = sorted(s for s in seconds if s is not None)
    if not values:
        print(f"{label}: no data yet")
        return
    print(
        f"{label}: median {statistics.median(values) / 60:.1f} minutes ({len(values)} businesses)"
    )


def main() -> int:
    """Print per-intake accuracy and the overall correction rate."""
    with Session(get_engine()) as session:
        rows = session.exec(
            select(IntakeSession, Business)
            .join(Business, col(Business.id) == col(IntakeSession.business_id))
            .where(col(Business.is_sample) == False, col(IntakeSession.status) == "done")  # noqa: E712
        ).all()
    pairs = [(i.first_guess or {}, i.draft or {}) for i, _ in rows]
    print(f"Real intakes confirmed: {len(pairs)}")
    for (intake, business), (guess, final) in zip(rows, pairs, strict=True):
        result = accuracy(guess, final)
        print(
            f"- business {business.id} ({intake.source}): fields {result.field_accuracy:.0%}, "
            f"prices {result.price_accuracy:.0%}, products missed {result.products_missed}, "
            f"made up {result.products_made_up}"
        )
    print(f"Correction rate (fields edited / all fields): {correction_rate(pairs):.1%}")
    intakes = [i for i, _ in rows]
    _median_minutes(
        "System time, voice note to draft scored", [i.seconds_to_draft_scored for i in intakes]
    )
    _median_minutes(
        "Voice note to live website (includes the owner's decision time)",
        [i.seconds_to_live for i in intakes],
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
