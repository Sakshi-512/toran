"""Measuring the agent's accuracy: what Claude first guessed vs what the owner confirmed.

Profiles are flattened into comparable fields (products matched by normalised name, options
by normalised label, prices compared exactly). Corrections are saved per intake; the report
totals them across real businesses only.
"""

import re
import unicodedata
from dataclasses import dataclass
from typing import Any

SET_FIELDS = ("delivery_areas", "languages", "order_methods")
TEXT_FIELDS = ("business_name", "business_type", "area", "lead_time", "phone", "policy")


def norm(text: str) -> str:
    """Comparable form: case-folded, punctuation and extra spaces removed."""
    text = unicodedata.normalize("NFC", text).casefold()
    return " ".join(re.sub(r"[^\w\s]", " ", text).split())


def flatten(profile: dict[str, Any]) -> dict[str, Any]:
    """One entry per comparable field, e.g. "products[chocolate cake].options[1 kg].price"."""
    flat: dict[str, Any] = {}
    for key in TEXT_FIELDS:
        value = str(profile.get(key) or "").strip()
        if value:
            flat[key] = value
    for key in SET_FIELDS:
        values = sorted({norm(str(v)) for v in profile.get(key) or [] if str(v).strip()})
        if values:
            flat[key] = values
    for product in profile.get("products") or []:
        base = f"products[{norm(product.get('name', ''))}]"
        flat[f"{base}.unit"] = product.get("unit")
        if product.get("notes"):
            flat[f"{base}.notes"] = product["notes"].strip()
        for option in product.get("options") or []:
            flat[f"{base}.options[{norm(option.get('label', ''))}].price"] = option.get("price")
    return flat


def corrections(before: dict[str, Any], after: dict[str, Any]) -> list[dict[str, Any]]:
    """Every field that differs, with its value before and after."""
    a, b = flatten(before), flatten(after)
    return [
        {"field": k, "before": a.get(k), "after": b.get(k)}
        for k in sorted(set(a) | set(b))
        if a.get(k) != b.get(k)
    ]


@dataclass(frozen=True)
class Accuracy:
    """Accuracy of one first guess against the confirmed profile."""

    fields_total: int
    fields_correct: int
    prices_total: int
    prices_exact: int
    products_missed: int
    products_made_up: int

    @property
    def field_accuracy(self) -> float:
        """Share of fields Claude got right (1.0 when there were none)."""
        return self.fields_correct / self.fields_total if self.fields_total else 1.0

    @property
    def price_accuracy(self) -> float:
        """Share of confirmed prices Claude got exactly right."""
        return self.prices_exact / self.prices_total if self.prices_total else 1.0


def _products(flat: dict[str, Any]) -> set[str]:
    return {k.split("]")[0] + "]" for k in flat if k.startswith("products[")}


def accuracy(first_guess: dict[str, Any], confirmed: dict[str, Any]) -> Accuracy:
    """Compare Claude's first guess with what the owner confirmed."""
    guess, final = flatten(first_guess), flatten(confirmed)
    keys = set(guess) | set(final)
    prices = [k for k in final if k.endswith(".price")]
    return Accuracy(
        fields_total=len(keys),
        fields_correct=sum(guess.get(k) == final.get(k) for k in keys),
        prices_total=len(prices),
        prices_exact=sum(guess.get(k) == final[k] for k in prices),
        products_missed=len(_products(final) - _products(guess)),
        products_made_up=len(_products(guess) - _products(final)),
    )


def correction_rate(pairs: list[tuple[dict[str, Any], dict[str, Any]]]) -> float:
    """Fields the owners edited divided by all fields, across many intakes."""
    total = edited = 0
    for guess, final in pairs:
        result = accuracy(guess, final)
        total += result.fields_total
        edited += result.fields_total - result.fields_correct
    return edited / total if total else 0.0
