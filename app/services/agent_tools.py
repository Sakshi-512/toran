"""The agent fix loop's tools, and the code checks every tool call must pass.

Only six tools exist (CLAUDE.md rule 9): run_tests, compress_images, add_translation,
edit_layout, ask_owner and rebuild_draft. Each call is validated here before anything changes:
- inputs are strict (unknown fields such as "price" or "phone" are rejected),
- a translation may only carry her confirmed text into another language: the same numbers,
  no prices or currency, no phone numbers or links, the right script, and a sensible length,
- layout choices come from a fixed list and can never touch a fact,
- questions are fixed profile topics, worded by our own templates in her language.
Nothing here can change her profile, publish, or spend money.
"""

import re
from dataclasses import dataclass, field, replace
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.schemas import BusinessProfile
from app.services.address_guard import address_reason
from app.services.site_extras import SECTIONS, Extras, Layout, Translation

TOOL_NAMES: tuple[str, ...] = (
    "run_tests",
    "compress_images",
    "add_translation",
    "edit_layout",
    "ask_owner",
    "rebuild_draft",
)
SITE_TOOLS = frozenset({"compress_images", "add_translation", "edit_layout"})
QUESTION_TOPICS: tuple[str, ...] = ("policy", "lead_time", "delivery_areas", "photos")
LAYOUT_CHANGES: tuple[str, ...] = (
    "main_button_first",
    "photo_after_text",
    "large_buttons",
    "section_order",
)
MAX_WHY = 300
MAX_TRANSLATION = 300

DEVANAGARI_DIGITS = str.maketrans("०१२३४५६७८९", "0123456789")
_DEVANAGARI = re.compile(r"[ऀ-ॿ]")
_LETTER = re.compile(r"[^\W\d_]")
_NUMBER = re.compile(r"\d+(?:[.,]\d+)?")
_FORBIDDEN = re.compile(
    r"₹|\brs\.?\b|\binr\b|rupee|रु\.?|रुपय|रुपए|रुपये|http|www\.|@|\+\d|/-",
    re.IGNORECASE,
)


class _Input(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    why: str = Field(min_length=1, max_length=MAX_WHY)


class RunTests(_Input):
    """Test the draft like a customer and score it again (ends the round)."""


class RebuildDraft(_Input):
    """Rebuild the draft from her profile plus the current improvements."""


class CompressImages(_Input):
    """Use lighter photo files so the page loads faster on slow internet."""


class AddTranslation(_Input):
    """Show her confirmed text in another site language."""

    target: Literal["product_name", "product_notes", "option_label"]
    product: int = Field(ge=1, le=100)
    option: int | None = Field(default=None, ge=1, le=20)
    lang: Literal["en", "hi", "mr"]
    text: str = Field(min_length=1, max_length=MAX_TRANSLATION)


class EditLayout(_Input):
    """Change how the page is arranged, never what it says."""

    change: Literal["main_button_first", "photo_after_text", "large_buttons", "section_order"]
    section_order: list[Literal["menu", "howto", "policy", "areas", "contact"]] | None = None


class AskOwner(_Input):
    """Save a question only she can answer."""

    topic: Literal["policy", "lead_time", "delivery_areas", "photos"]


INPUT_MODELS: dict[str, type[_Input]] = {
    "run_tests": RunTests,
    "rebuild_draft": RebuildDraft,
    "compress_images": CompressImages,
    "add_translation": AddTranslation,
    "edit_layout": EditLayout,
    "ask_owner": AskOwner,
}

_WHY = {"type": "string", "description": "One short sentence: why this helps her customers."}


def _tool(
    name: str, description: str, properties: dict[str, Any], required: list[str]
) -> dict[str, Any]:
    return {
        "name": name,
        "description": description,
        "input_schema": {
            "type": "object",
            "properties": {**properties, "why": _WHY},
            "required": [*required, "why"],
            "additionalProperties": False,
        },
    }


TOOL_DEFINITIONS: list[dict[str, Any]] = [
    _tool(
        "add_translation",
        "Show one piece of her confirmed text (a product name, its notes, or a price option "
        "label) in another site language. Translate only; never add, drop or change numbers, "
        "prices, sizes, ingredients or claims. Prices are added by the site, not by you.",
        {
            "target": {"type": "string", "enum": ["product_name", "product_notes", "option_label"]},
            "product": {"type": "integer", "minimum": 1, "description": "Product number, from 1"},
            "option": {
                "type": "integer",
                "minimum": 1,
                "description": "Price option number, from 1 (only for option_label)",
            },
            "lang": {"type": "string", "enum": ["en", "hi", "mr"]},
            "text": {"type": "string", "description": "The translated text"},
        },
        ["target", "product", "lang", "text"],
    ),
    _tool(
        "edit_layout",
        "Change the page layout. main_button_first: the order button goes right under the "
        "business name. photo_after_text: on phones the photo moves below the name and button. "
        "large_buttons: bigger buttons. section_order: a new order for menu, howto, policy, "
        "areas and contact (all five, each once).",
        {
            "change": {"type": "string", "enum": list(LAYOUT_CHANGES)},
            "section_order": {
                "type": "array",
                "items": {"type": "string", "enum": list(SECTIONS)},
                "description": "Only for change=section_order",
            },
        },
        ["change"],
    ),
    _tool(
        "compress_images",
        "Use lighter photo files so the page loads faster on slow internet.",
        {},
        [],
    ),
    _tool(
        "ask_owner",
        "Save a question for the owner when only she knows the answer: her ordering policy "
        "(advance, cancellation, delivery charges), how much notice she needs (lead_time), "
        "where she delivers (delivery_areas), or real photos of her products (photos).",
        {"topic": {"type": "string", "enum": list(QUESTION_TOPICS)}},
        ["topic"],
    ),
    _tool(
        "rebuild_draft",
        "Rebuild the draft with your changes. Happens anyway at the end of the round.",
        {},
        [],
    ),
    _tool(
        "run_tests",
        "Finish this round: rebuild the draft and test it like a customer to get the new score.",
        {},
        [],
    ),
]


@dataclass
class ToolOutcome:
    """What one tool call did. message goes back to Claude; change is shown to the owner."""

    tool: str
    ok: bool
    message: str
    why: str = ""
    extras: Extras | None = None
    change: dict[str, Any] = field(default_factory=dict)
    question: str | None = None
    raw_input: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class SiteState:
    """What the tools may look at: her confirmed profile and the site's current state."""

    profile: BusinessProfile
    extras: Extras
    photo_count: int
    open_topics: frozenset[str] = frozenset()


def _numbers(text: str) -> list[str]:
    plain = text.translate(DEVANAGARI_DIGITS).replace(",", "")
    return sorted(_NUMBER.findall(plain))


def _is_devanagari(text: str) -> bool:
    letters = _LETTER.findall(text)
    if not letters:
        return False
    return sum(1 for ch in letters if _DEVANAGARI.match(ch)) * 2 >= len(letters)


def translation_problem(source: str, text: str, lang: str) -> str | None:
    """Why a translation is not allowed, or None if it is fine."""
    if text.strip() == source.strip():
        return "The text is the same as her original."
    if _numbers(text) != _numbers(source):
        return "Numbers must stay exactly as in her text. Never add, drop or change a number."
    if _FORBIDDEN.search(text):
        return "No prices, currency, phone numbers or links. The site adds prices from her profile."
    if len(text) > max(60, 3 * len(source)):
        return "Too long for a translation of her text. Translate only what she wrote."
    if lang == "en" and _DEVANAGARI.search(text):
        return "An English translation must be in English letters."
    if lang in ("hi", "mr") and not _is_devanagari(text):
        return "A Hindi or Marathi translation must be in Devanagari script."
    if address_reason(text):
        return "This looks like an address. Never show a home address."
    return None


def _source_text(profile: BusinessProfile, call: AddTranslation) -> tuple[str | None, str]:
    """The original text a translation is for, or None with the reason."""
    if call.product > len(profile.products):
        return None, f"There is no product {call.product}."
    product = profile.products[call.product - 1]
    if call.target == "product_name":
        return product.name, ""
    if call.target == "product_notes":
        return (product.notes, "") if product.notes else (None, "This product has no notes.")
    if call.option is None or call.option > len(product.options):
        return None, "Give the price option number for option_label."
    label = product.options[call.option - 1].label
    return (label, "") if label else (None, "This price option has no label to translate.")


def _rejected(tool: str, message: str, raw: dict[str, Any], why: str = "") -> ToolOutcome:
    return ToolOutcome(tool, False, f"Rejected: {message}", why=why, raw_input=raw)


def apply_tool(name: str, raw: dict[str, Any], state: SiteState) -> ToolOutcome:
    """Validate one tool call and work out its effect. Never changes her profile."""
    raw = raw if isinstance(raw, dict) else {}
    model = INPUT_MODELS.get(name)
    if model is None:
        return _rejected(name[:40], "unknown tool. Only the six listed tools exist.", raw)
    try:
        call = model.model_validate(raw)
    except ValidationError as exc:
        fields = sorted({str(e["loc"][0]) for e in exc.errors() if e.get("loc")})
        return _rejected(
            name,
            "these inputs are not allowed or not valid: "
            + ", ".join(fields)
            + ". Tools can never change prices, products, phone, area or other facts.",
            raw,
        )
    if isinstance(call, AddTranslation):
        return _translate(call, state, raw)
    if isinstance(call, EditLayout):
        return _layout(call, state, raw)
    if isinstance(call, CompressImages):
        return _compress(call, state, raw)
    if isinstance(call, AskOwner):
        return _ask(call, state, raw)
    return ToolOutcome(name, True, "OK", why=call.why, raw_input=raw)


def _translate(call: AddTranslation, state: SiteState, raw: dict[str, Any]) -> ToolOutcome:
    source, reason = _source_text(state.profile, call)
    if source is None:
        return _rejected("add_translation", reason, raw, call.why)
    problem = translation_problem(source, call.text, call.lang)
    if problem:
        return _rejected("add_translation", problem, raw, call.why)
    item = Translation(call.target, source, call.lang, call.text)
    return ToolOutcome(
        "add_translation",
        True,
        "Saved. It shows on the draft after this round.",
        why=call.why,
        extras=state.extras.with_translation(item),
        change={"kind": "translation", **item.to_json()},
        raw_input=raw,
    )


def _layout(call: EditLayout, state: SiteState, raw: dict[str, Any]) -> ToolOutcome:
    layout = state.extras.layout
    if call.change == "section_order":
        order = tuple(call.section_order or ())
        if sorted(order) != sorted(SECTIONS):
            return _rejected(
                "edit_layout", "section_order must list all five sections once.", raw, call.why
            )
        if order == layout.section_order:
            return _rejected(
                "edit_layout", "The sections are already in that order.", raw, call.why
            )
        new = replace(layout, section_order=order)
    else:
        if getattr(layout, call.change):
            return _rejected("edit_layout", "That change is already made.", raw, call.why)
        new = _switch_on(layout, call.change)
    return ToolOutcome(
        "edit_layout",
        True,
        "Saved. It shows on the draft after this round.",
        why=call.why,
        extras=state.extras.with_layout(new),
        change={"kind": "layout", "change": call.change, "order": list(new.section_order)},
        raw_input=raw,
    )


def _switch_on(layout: Layout, change: str) -> Layout:
    if change == "main_button_first":
        return replace(layout, main_button_first=True)
    if change == "photo_after_text":
        return replace(layout, photo_after_text=True)
    return replace(layout, large_buttons=True)


def _compress(call: CompressImages, state: SiteState, raw: dict[str, Any]) -> ToolOutcome:
    layout = state.extras.layout
    if state.photo_count == 0:
        return _rejected("compress_images", "This site has no photos of hers.", raw, call.why)
    if layout.small_images:
        return _rejected("compress_images", "Photos are already compressed.", raw, call.why)
    new = replace(layout, small_images=True)
    return ToolOutcome(
        "compress_images",
        True,
        "Saved. Lighter photos are used after this round.",
        why=call.why,
        extras=state.extras.with_layout(new),
        change={"kind": "images"},
        raw_input=raw,
    )


def _ask(call: AskOwner, state: SiteState, raw: dict[str, Any]) -> ToolOutcome:
    profile = state.profile
    already = {
        "policy": bool(profile.policy),
        "lead_time": bool(profile.lead_time),
        "delivery_areas": bool(profile.delivery_areas),
        "photos": False,
    }
    if already[call.topic]:
        return _rejected("ask_owner", "She has already given this.", raw, call.why)
    if call.topic in state.open_topics:
        return _rejected("ask_owner", "This question is already waiting for her.", raw, call.why)
    return ToolOutcome(
        "ask_owner",
        True,
        "Saved. She will see the question on her page; the site will not change until she answers.",
        why=call.why,
        change={"kind": "question", "topic": call.topic},
        question=call.topic,
        raw_input=raw,
    )
