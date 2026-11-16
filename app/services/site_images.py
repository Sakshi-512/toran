"""Images for business sites: uploads and bundled sample photos become resized WebP files."""

import hashlib
import io
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from PIL import Image

from app.services.uploads import check_image

# Free stock photos used only by sample businesses (credits in docs/photo-credits.md).
SAMPLE_PHOTO_DIR = Path(__file__).resolve().parent.parent / "templates" / "sites" / "sample-photos"
WIDTHS = (480, 960)
WEBP_QUALITIES = (78, 65, 50, 35, 20)
# Byte budget per image width, so photos cannot push a page past the 500 KB limit.
BYTE_BUDGET = {480: 40 * 1024, 960: 90 * 1024}
# compress_images (agent tool): lighter photos for slow connections.
SMALL_BUDGET_SHARE = 0.6


@dataclass(frozen=True)
class SitePhoto:
    """A photo for a site. subject is a product name, or None for the business itself.

    Exactly one of data (an uploaded image) or sample_photo (a bundled file name) is set.
    """

    subject: str | None = None
    data: bytes | None = None
    sample_photo: str | None = None

    @property
    def is_sample(self) -> bool:
        """True for bundled stock photos shown with a "Sample photo" label."""
        return self.sample_photo is not None


@dataclass(frozen=True)
class ImageRef:
    """Where a prepared image lives in the site, relative to the site root."""

    src: str
    srcset: tuple[tuple[str, int], ...]
    width: int
    height: int


def _short_hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()[:10]


def prepare_photo(
    photo: SitePhoto, stem: str, small: bool = False
) -> tuple[ImageRef, dict[str, bytes]]:
    """Return the image reference and the files to write for one photo (small: lighter files)."""
    if photo.sample_photo is not None:
        data = sample_photo_bytes(photo.sample_photo)
    elif photo.data is not None:
        data = photo.data
    else:
        raise ValueError("SitePhoto needs data or sample_photo")
    ref, files = _prepare_raster(data, stem, small)
    return ref, dict(files)


def sample_photo_bytes(name: str) -> bytes:
    """Read one bundled sample photo. Only plain .jpg names inside SAMPLE_PHOTO_DIR are allowed."""
    path = (SAMPLE_PHOTO_DIR / name).resolve()
    if path.parent != SAMPLE_PHOTO_DIR.resolve() or path.suffix != ".jpg" or not path.is_file():
        raise ValueError("unknown sample photo")
    return path.read_bytes()


@lru_cache(maxsize=32)
def _prepare_raster(
    data: bytes, stem: str, small: bool = False
) -> tuple[ImageRef, tuple[tuple[str, bytes], ...]]:
    """Check, strip and encode one image (cached: sample sites are rebuilt often)."""
    checked = check_image(data)
    files: dict[str, bytes] = {}
    srcset: list[tuple[str, int]] = []
    with Image.open(io.BytesIO(checked.data)) as img:
        for width in WIDTHS:
            if srcset and srcset[-1][1] >= img.width:
                break  # already have the image at its full width; never upscale
            encoded = _to_webp(img, width, small)
            target = f"assets/img/{stem}-{width}.{_short_hash(encoded)}.webp"
            files[target] = encoded
            srcset.append((target, min(width, img.width)))
        height = round(img.height * srcset[0][1] / img.width)
    return ImageRef(srcset[0][0], tuple(srcset), srcset[0][1], height), tuple(files.items())


def _to_webp(img: Image.Image, width: int, small: bool = False) -> bytes:
    """Encode at the highest quality that fits the byte budget for this width."""
    budget = BYTE_BUDGET[width] * (SMALL_BUDGET_SHARE if small else 1.0)
    copy = img.copy()
    copy.thumbnail((width, width * 4))
    encoded = b""
    for quality in WEBP_QUALITIES:
        out = io.BytesIO()
        copy.save(out, format="WEBP", quality=quality, method=4)
        encoded = out.getvalue()
        if len(encoded) <= budget:
            break
    return encoded
