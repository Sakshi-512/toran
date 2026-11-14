"""Helpers for building and reading generated sites in tests."""

import re
from datetime import date
from html.parser import HTMLParser
from typing import Any

from app.schemas import BusinessProfile
from app.services.samples import get_sample
from app.services.site_images import SitePhoto
from app.services.sitegen import SiteBuild, SiteInput, build_site

SITE_URL = "https://shop.example.test"
MAIN_URL = "https://example.test"
UPDATED = date(2026, 11, 14)
PAGES = ("index.html", "hi/index.html", "mr/index.html")
ALL_PAGES = (*PAGES, "thanks/index.html", "hi/thanks/index.html", "mr/thanks/index.html")


def profile(**overrides: Any) -> BusinessProfile:
    """A real-looking (non-sample) bakery profile."""
    data: dict[str, Any] = {
        "business_name": "Priya Cakes",
        "business_type": "bakery",
        "area": "Baner, Pune",
        "delivery_areas": ["Baner", "Aundh"],
        "languages": ["en", "mr"],
        "products": [
            {
                "name": "Chocolate cake",
                "unit": "kg",
                "options": [{"label": "1 kg", "price": 1250}, {"label": "3 kg", "price": 125000}],
                "notes": "",
            },
            {
                "name": "Cupcakes",
                "unit": "box",
                "options": [{"label": "", "price": 300}],
                "notes": "Box of 6",
            },
        ],
        "order_methods": ["whatsapp", "call"],
        "lead_time": "3 days",
        "phone": "9876543210",
        "missing": [],
    }
    data.update(overrides)
    return BusinessProfile.model_validate(data)


def build(
    prof: BusinessProfile | None = None,
    theme: str = "warm",
    is_sample: bool = False,
    photos: tuple[SitePhoto, ...] = (),
) -> SiteBuild:
    """Build a site for a profile."""
    return build_site(
        SiteInput(
            slug="shop",
            display_name="Shop",
            profile=prof or profile(),
            theme=theme,
            is_sample=is_sample,
            prices_updated=UPDATED,
            site_url=SITE_URL,
            main_url=MAIN_URL,
            enquiry_url=f"{MAIN_URL}/api/enquiry/shop",
            app_name="Test App",
            photos=photos,
        )
    )


def build_sample(slug: str, theme: str | None = None) -> tuple[SiteBuild, BusinessProfile]:
    """Build one of the sample businesses."""
    sample = get_sample(slug)
    assert sample is not None
    return (
        build(sample.profile, theme or sample.theme, is_sample=True, photos=sample.photos),
        sample.profile,
    )


class Page(HTMLParser):
    """Parsed page: visible text, tags with attributes, and text by CSS class."""

    def __init__(self, html: str) -> None:
        super().__init__()
        self.html = html
        self.tags: list[tuple[str, dict[str, str]]] = []
        self.text_parts: list[str] = []
        self.by_class: dict[str, list[str]] = {}
        self._open: list[str | None] = []
        self.feed(html)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = {k: v or "" for k, v in attrs}
        self.tags.append((tag, a))
        if tag not in ("img", "input", "link", "meta", "br"):
            self._open.append(a.get("class"))

    def handle_endtag(self, tag: str) -> None:
        if self._open and tag not in ("img", "input", "link", "meta", "br"):
            self._open.pop()

    def handle_data(self, data: str) -> None:
        self.text_parts.append(data)
        cls = self._open[-1] if self._open else None
        if cls and data.strip():
            self.by_class.setdefault(cls, []).append(data.strip())

    @property
    def text(self) -> str:
        """All visible text."""
        return " ".join(self.text_parts)

    def find(self, tag: str) -> list[dict[str, str]]:
        """Attributes of every tag with this name."""
        return [a for t, a in self.tags if t == tag]


def page(site: SiteBuild, path: str = "index.html") -> Page:
    """Parse one generated page."""
    return Page(site.files[path].decode("utf-8"))


def numbers_in(text: str) -> set[str]:
    """Digit runs in text, ignoring Indian thousands separators."""
    return {m.replace(",", "") for m in re.findall(r"\d[\d,]*\d|\d", text)}
