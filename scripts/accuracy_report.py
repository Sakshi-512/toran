"""Accuracy from real use: Claude's first guess vs what owners confirmed (real businesses only).

Run: python -m scripts.accuracy_report (or make accuracy). Add --fields to list which fields each
owner edited: field names only, never their values, with product and option names masked.
"""

import statistics
import sys

from sqlmodel import Session, col, select

from app.db import get_engine
from app.models import Business, IntakeSession
from app.services.accuracy import accuracy, correction_rate, corrections


def _median_minutes(label: str, seconds: list[int | None]) -> None:
    values = sorted(s for s in seconds if s is not None)
    if not values:
        print(f"{label}: no data yet")
        return
    print(
        f"{label}: median {statistics.median(values) / 60:.1f} minutes ({len(values)} businesses)"
    )


def _masked(field: str, names: dict[str, str]) -> str:
    """A field name with product and option names replaced by "product 1", "option 2"..."""

    def mask(prefix: str, name: str) -> str:
        key = f"{prefix}[{name}]"
        if key not in names:
            count = sum(k.startswith(prefix + "[") for k in names) + 1
            names[key] = f"{prefix.rstrip('s')} {count}"
        return names[key]

    out = []
    for part in field.split("."):
        if "[" in part:
            prefix, name = part.split("[", 1)
            out.append(mask(prefix, name.rstrip("]")))
        else:
            out.append(part)
    return ".".join(out)


def main() -> int:
    """Print per-intake accuracy and the overall correction rate."""
    show_fields = "--fields" in sys.argv[1:]
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
            f"- business {business.id} ({intake.source}): "
            f"fields {result.fields_correct}/{result.fields_total} ({result.field_accuracy:.0%}), "
            f"prices {result.prices_exact}/{result.prices_total} ({result.price_accuracy:.0%}), "
            f"products missed {result.products_missed}, made up {result.products_made_up}"
        )
        if show_fields:
            names: dict[str, str] = {}
            for change in corrections(guess, final):
                was = "set" if change["before"] is not None else "empty"
                now = "set" if change["after"] is not None else "empty"
                print(f"    edited: {_masked(change['field'], names)} ({was} -> {now})")
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
