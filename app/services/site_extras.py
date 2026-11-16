"""Agent improvements to a site, kept apart from the owner's confirmed profile.

Two kinds, both checked before they are saved (app/services/agent_tools.py):
- translations of her confirmed text (product names, notes and option labels) into en, hi or
  mr, keyed by a hash of the original, so a translation disappears by itself if she edits the
  original text. Prices and numbers are never translated: they still come from the profile.
- layout choices from a fixed list (main button first, photo after text, large buttons, section
  order, lighter photos). None of them can change a fact.

Every change is a new SiteExtras version. The newest version that is not discarded is used for
every draft build; publishing that draft is her approval (rule 2).
"""

import hashlib
from dataclasses import dataclass, field, replace
from typing import Any

from sqlmodel import Session, col, select

from app.models import SiteExtras

SECTIONS: tuple[str, ...] = ("menu", "howto", "policy", "areas", "contact")
FIELDS: tuple[str, ...] = ("product_name", "product_notes", "option_label")


def source_key(text: str) -> str:
    """Short hash of the original text a translation belongs to."""
    return hashlib.sha256(text.strip().encode()).hexdigest()[:16]


@dataclass(frozen=True)
class Layout:
    """Layout choices the agent may make. The defaults are the normal site."""

    main_button_first: bool = False
    photo_after_text: bool = False
    large_buttons: bool = False
    small_images: bool = False
    section_order: tuple[str, ...] = SECTIONS

    def to_json(self) -> dict[str, Any]:
        """For the database."""
        return {
            "main_button_first": self.main_button_first,
            "photo_after_text": self.photo_after_text,
            "large_buttons": self.large_buttons,
            "small_images": self.small_images,
            "section_order": list(self.section_order),
        }

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> "Layout":
        """From the database; anything unknown falls back to the default."""
        order = tuple(s for s in data.get("section_order", ()) if s in SECTIONS)
        if sorted(order) != sorted(SECTIONS):
            order = SECTIONS
        return cls(
            main_button_first=bool(data.get("main_button_first")),
            photo_after_text=bool(data.get("photo_after_text")),
            large_buttons=bool(data.get("large_buttons")),
            small_images=bool(data.get("small_images")),
            section_order=order,
        )


@dataclass(frozen=True)
class Translation:
    """One translation of her confirmed text into one site language."""

    field: str
    source: str
    lang: str
    text: str

    @property
    def key(self) -> tuple[str, str]:
        """(source hash, language)."""
        return (source_key(self.source), self.lang)

    def to_json(self) -> dict[str, str]:
        """For the database."""
        return {"field": self.field, "source": self.source, "lang": self.lang, "text": self.text}


@dataclass(frozen=True)
class Extras:
    """All agent improvements for one site."""

    translations: tuple[Translation, ...] = ()
    layout: Layout = field(default_factory=Layout)

    def translate(self, text: str, lang: str) -> str | None:
        """Her text on a page in `lang`, if the agent added a translation for it."""
        wanted = (source_key(text), lang)
        for item in reversed(self.translations):
            if item.key == wanted:
                return item.text
        return None

    def with_translation(self, item: Translation) -> "Extras":
        """A copy with this translation added (replacing one for the same text and language)."""
        kept = tuple(t for t in self.translations if t.key != item.key)
        return replace(self, translations=(*kept, item))

    def with_layout(self, layout: Layout) -> "Extras":
        """A copy with new layout choices."""
        return replace(self, layout=layout)


EMPTY = Extras()


def _from_row(row: SiteExtras) -> Extras:
    items = tuple(
        Translation(str(t["field"]), str(t["source"]), str(t["lang"]), str(t["text"]))
        for t in row.translations
        if isinstance(t, dict) and {"field", "source", "lang", "text"} <= set(t)
    )
    return Extras(items, Layout.from_json(row.layout or {}))


def current_row(session: Session, business_id: int) -> SiteExtras | None:
    """The newest version that is not discarded."""
    return session.exec(
        select(SiteExtras)
        .where(SiteExtras.business_id == business_id, SiteExtras.status != "discarded")
        .order_by(col(SiteExtras.version).desc())
    ).first()


def current_extras(session: Session, business_id: int) -> Extras:
    """The improvements every draft build uses (none if the agent never ran)."""
    row = current_row(session, business_id)
    return _from_row(row) if row else EMPTY


def current_version(session: Session, business_id: int) -> int | None:
    """Version number of the current improvements, if any."""
    row = current_row(session, business_id)
    return row.version if row else None


def save_version(
    session: Session,
    business_id: int,
    extras: Extras,
    agent_run_id: str | None = None,
    round_number: int = 0,
) -> int:
    """Store a new version (proposed) and return its number."""
    numbers = session.exec(
        select(SiteExtras.version).where(SiteExtras.business_id == business_id)
    ).all()
    number = max(numbers, default=0) + 1
    session.add(
        SiteExtras(
            business_id=business_id,
            version=number,
            agent_run_id=agent_run_id,
            round=round_number,
            translations=[t.to_json() for t in extras.translations],
            layout=extras.layout.to_json(),
        )
    )
    session.commit()
    return number


def discard_after(session: Session, business_id: int, version: int | None) -> int:
    """Discard every version newer than `version` (None discards all). Returns how many."""
    rows = session.exec(
        select(SiteExtras).where(
            SiteExtras.business_id == business_id, SiteExtras.status != "discarded"
        )
    ).all()
    changed = 0
    for row in rows:
        if version is None or row.version > version:
            row.status = "discarded"
            session.add(row)
            changed += 1
    session.commit()
    return changed


def approve_current(session: Session, business_id: int) -> None:
    """She published the draft: every proposed version is approved (the caller commits)."""
    rows = session.exec(
        select(SiteExtras).where(
            SiteExtras.business_id == business_id, SiteExtras.status == "proposed"
        )
    ).all()
    for row in rows:
        row.status = "approved"
        session.add(row)


def discard_run(session: Session, business_id: int, agent_run_id: str) -> int:
    """She undid an agent run: discard every version it made. Returns how many."""
    rows = session.exec(
        select(SiteExtras).where(
            SiteExtras.business_id == business_id,
            SiteExtras.agent_run_id == agent_run_id,
            SiteExtras.status != "discarded",
        )
    ).all()
    for row in rows:
        row.status = "discarded"
        session.add(row)
    session.commit()
    return len(rows)
