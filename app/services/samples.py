"""Sample businesses (is_sample) used for demos, the theme gallery and tests."""

import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from app.schemas import BusinessProfile
from app.services.site_images import SitePhoto

SAMPLES_DIR = Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "profiles"


@dataclass(frozen=True)
class Sample:
    """One sample business: its record fields, profile and illustration photos."""

    slug: str
    display_name: str
    theme: str
    profile: BusinessProfile
    photos: tuple[SitePhoto, ...]


def _load(path: Path) -> Sample:
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("is_sample") is not True:
        raise ValueError(f"{path.name} must set is_sample: true")
    photos = tuple(
        SitePhoto(subject=p.get("subject"), sample_svg=p["sample_svg"]) for p in data["photos"]
    )
    return Sample(
        slug=data["slug"],
        display_name=data["display_name"],
        theme=data["theme"],
        profile=BusinessProfile.model_validate(data["profile"]),
        photos=photos,
    )


@lru_cache
def load_samples() -> tuple[Sample, ...]:
    """All sample businesses, sorted by slug."""
    return tuple(_load(p) for p in sorted(SAMPLES_DIR.glob("*.json")))


def get_sample(slug: str) -> Sample | None:
    """One sample by slug."""
    return next((s for s in load_samples() if s.slug == slug), None)
