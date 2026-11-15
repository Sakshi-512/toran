"""Customer questions per business type, read from docs/checklists.md (the single source)."""

import re
from functools import lru_cache
from pathlib import Path

CHECKLISTS_FILE = Path(__file__).resolve().parents[2] / "docs" / "checklists.md"
HEADINGS = {
    "bakery": "Bakery",
    "tailoring": "Tailoring",
    "tuition": "Tuition",
    "mehendi": "Mehendi",
    "beauty": "Beauty parlour",
    "other": "General (any other business)",
}


@lru_cache
def load_checklists(path: Path = CHECKLISTS_FILE) -> dict[str, tuple[str, ...]]:
    """Heading -> questions, parsed from the markdown file."""
    sections: dict[str, list[str]] = {}
    current: list[str] | None = None
    for line in path.read_text(encoding="utf-8").splitlines():
        heading = re.match(r"^##\s+(.+?)\s*$", line)
        if heading:
            current = sections.setdefault(heading.group(1), [])
        elif current is not None and line.startswith("- "):
            current.append(line[2:].strip())
    return {name: tuple(questions) for name, questions in sections.items()}


def checklist_for(business_type: str) -> tuple[str, ...]:
    """The customer questions for a business type (General for anything unknown)."""
    lists = load_checklists()
    return lists.get(HEADINGS.get(business_type, HEADINGS["other"]), lists[HEADINGS["other"]])
