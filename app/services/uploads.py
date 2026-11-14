"""Upload checks: allowed types by content (not file name), size limits, image re-encoding.

Re-encoding with Pillow drops all metadata, including EXIF and GPS location.
"""

import io
from dataclasses import dataclass

from PIL import Image, ImageOps, UnidentifiedImageError

from app.services.errors import AppError

MAX_IMAGE_BYTES = 8 * 1024 * 1024
MAX_AUDIO_BYTES = 15 * 1024 * 1024
MAX_IMAGE_PIXELS = 40_000_000
MAX_IMAGE_SIDE = 2400

_IMAGE_FORMATS = {"JPEG": "image/jpeg", "PNG": "image/png", "WEBP": "image/webp"}


class UploadRejected(AppError):
    """The upload failed a check; key is the i18n message for the owner."""

    def __init__(self, key: str) -> None:
        super().__init__(key, status_code=400)


@dataclass(frozen=True)
class CheckedImage:
    """A safe, re-encoded image with no metadata."""

    data: bytes
    mime: str
    width: int
    height: int


@dataclass(frozen=True)
class CheckedAudio:
    """An audio upload whose type and size passed the checks."""

    data: bytes
    mime: str


def sniff_image(data: bytes) -> str | None:
    """Return the image format from magic bytes, or None if not an allowed image."""
    if data.startswith(b"\xff\xd8\xff"):
        return "JPEG"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "PNG"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "WEBP"
    return None


def sniff_audio(data: bytes) -> str | None:
    """Return the audio MIME type from magic bytes, or None if not an allowed audio type."""
    if data.startswith(b"OggS"):
        return "audio/ogg"
    if data.startswith(b"\x1a\x45\xdf\xa3"):
        return "audio/webm"
    if data[:4] == b"RIFF" and data[8:12] == b"WAVE":
        return "audio/wav"
    if data.startswith(b"ID3") or (len(data) > 1 and data[0] == 0xFF and data[1] & 0xE0 == 0xE0):
        return "audio/mpeg"
    if data[4:8] == b"ftyp":
        return "audio/mp4"
    return None


def _check_size(data: bytes, limit: int) -> None:
    if not data:
        raise UploadRejected("upload.empty")
    if len(data) > limit:
        raise UploadRejected("upload.too_large")


def check_image(data: bytes) -> CheckedImage:
    """Validate an uploaded photo and return a re-encoded copy without EXIF or GPS data."""
    _check_size(data, MAX_IMAGE_BYTES)
    fmt = sniff_image(data)
    if fmt is None:
        raise UploadRejected("upload.bad_type")
    try:
        with Image.open(io.BytesIO(data)) as probe:
            if probe.width * probe.height > MAX_IMAGE_PIXELS:
                raise UploadRejected("upload.too_large")
            probe.verify()
        with Image.open(io.BytesIO(data)) as img:
            img.load()
            clean = _strip_and_resize(img)
    except UploadRejected:
        raise
    except (UnidentifiedImageError, OSError, SyntaxError, ValueError) as exc:
        raise UploadRejected("upload.bad_image") from exc
    return _encode(clean, fmt)


def _strip_and_resize(img: Image.Image) -> Image.Image:
    rotated = ImageOps.exif_transpose(img)
    mode = "RGBA" if rotated.mode in ("RGBA", "LA", "P") else "RGB"
    clean = Image.new(mode, rotated.size)
    clean.paste(rotated.convert(mode))
    clean.thumbnail((MAX_IMAGE_SIDE, MAX_IMAGE_SIDE))
    return clean


def _encode(img: Image.Image, fmt: str) -> CheckedImage:
    out = io.BytesIO()
    if fmt == "JPEG":
        img.convert("RGB").save(out, format="JPEG", quality=85, optimize=True)
    else:
        img.save(out, format=fmt)
    return CheckedImage(out.getvalue(), _IMAGE_FORMATS[fmt], img.width, img.height)


def check_audio(data: bytes) -> CheckedAudio:
    """Validate an uploaded voice note by content type and size. Audio is never logged."""
    _check_size(data, MAX_AUDIO_BYTES)
    mime = sniff_audio(data)
    if mime is None:
        raise UploadRejected("upload.bad_type")
    return CheckedAudio(data, mime)
