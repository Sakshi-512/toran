"""Images for business sites: uploads become resized WebP files; samples use built-in SVGs."""

import hashlib
import io
from dataclasses import dataclass
from pathlib import Path

from PIL import Image

from app.services.uploads import check_image

SAMPLE_SVG_DIR = Path(__file__).resolve().parent.parent / "templates" / "sites" / "img"
WIDTHS = (480, 960)
WEBP_QUALITIES = (78, 65, 50, 35, 20)
# Byte budget per image width, so photos cannot push a page past the 500 KB limit.
BYTE_BUDGET = {480: 50 * 1024, 960: 110 * 1024}


@dataclass(frozen=True)
class SitePhoto:
    """A photo for a site. subject is a product name, or None for the business itself.

    Exactly one of data (an uploaded raster image) or sample_svg (a built-in file name) is set.
    """

    subject: str | None = None
    data: bytes | None = None
    sample_svg: str | None = None

    @property
    def is_sample(self) -> bool:
        """True for built-in "Sample photo" illustrations."""
        return self.sample_svg is not None


@dataclass(frozen=True)
class ImageRef:
    """Where a prepared image lives in the site, relative to the site root."""

    src: str
    srcset: tuple[tuple[str, int], ...]
    width: int
    height: int


def _short_hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()[:10]


def prepare_photo(photo: SitePhoto, stem: str) -> tuple[ImageRef, dict[str, bytes]]:
    """Return the image reference and the files to write for one photo."""
    if photo.sample_svg is not None:
        return _prepare_svg(photo.sample_svg, stem)
    if photo.data is None:
        raise ValueError("SitePhoto needs data or sample_svg")
    return _prepare_raster(photo.data, stem)


def _prepare_svg(name: str, stem: str) -> tuple[ImageRef, dict[str, bytes]]:
    path = (SAMPLE_SVG_DIR / name).resolve()
    if path.parent != SAMPLE_SVG_DIR.resolve() or path.suffix != ".svg":
        raise ValueError("unknown sample illustration")
    data = path.read_bytes()
    target = f"assets/img/{stem}.{_short_hash(data)}.svg"
    return ImageRef(target, (), 800, 600), {target: data}


def _prepare_raster(data: bytes, stem: str) -> tuple[ImageRef, dict[str, bytes]]:
    checked = check_image(data)
    files: dict[str, bytes] = {}
    srcset: list[tuple[str, int]] = []
    with Image.open(io.BytesIO(checked.data)) as img:
        for width in WIDTHS:
            if width > img.width and srcset:
                break
            encoded = _to_webp(img, width)
            target = f"assets/img/{stem}-{width}.{_short_hash(encoded)}.webp"
            files[target] = encoded
            srcset.append((target, min(width, img.width)))
        height = round(img.height * srcset[0][1] / img.width)
    return ImageRef(srcset[0][0], tuple(srcset), srcset[0][1], height), files


def _to_webp(img: Image.Image, width: int) -> bytes:
    """Encode at the highest quality that fits the byte budget for this width."""
    copy = img.copy()
    copy.thumbnail((width, width * 4))
    encoded = b""
    for quality in WEBP_QUALITIES:
        out = io.BytesIO()
        copy.save(out, format="WEBP", quality=quality, method=4)
        encoded = out.getvalue()
        if len(encoded) <= BYTE_BUDGET[width]:
            break
    return encoded
