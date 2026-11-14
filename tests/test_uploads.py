import io

import pytest
from PIL import ExifTags, Image

from app.services.uploads import MAX_IMAGE_BYTES, UploadRejected, check_audio, check_image


def _jpeg_with_gps() -> bytes:
    img = Image.new("RGB", (64, 48), (200, 100, 50))
    exif = Image.Exif()
    exif[ExifTags.Base.Make] = "PhoneMaker"
    exif[ExifTags.IFD.GPSInfo] = {1: "N", 2: (18.0, 31.0, 0.0), 3: "E", 4: (73.0, 51.0, 0.0)}
    out = io.BytesIO()
    img.save(out, format="JPEG", exif=exif)
    return out.getvalue()


def test_exif_and_gps_are_stripped() -> None:
    original = _jpeg_with_gps()
    assert Image.open(io.BytesIO(original)).getexif()
    checked = check_image(original)
    assert checked.mime == "image/jpeg"
    reopened = Image.open(io.BytesIO(checked.data))
    assert not reopened.getexif()
    assert b"PhoneMaker" not in checked.data


def test_png_and_webp_allowed() -> None:
    for fmt, mime in (("PNG", "image/png"), ("WEBP", "image/webp")):
        out = io.BytesIO()
        Image.new("RGBA", (10, 10)).save(out, format=fmt)
        assert check_image(out.getvalue()).mime == mime


def test_large_images_are_resized() -> None:
    out = io.BytesIO()
    Image.new("RGB", (4000, 3000)).save(out, format="JPEG")
    checked = check_image(out.getvalue())
    assert max(checked.width, checked.height) == 2400


@pytest.mark.parametrize(
    ("data", "key"),
    [
        (b"", "upload.empty"),
        (b"<svg xmlns='http://www.w3.org/2000/svg'></svg>", "upload.bad_type"),
        (b"%PDF-1.7 fake", "upload.bad_type"),
        (b"\xff\xd8\xff" + b"not really a jpeg", "upload.bad_image"),
        (b"\xff\xd8\xff" + b"0" * MAX_IMAGE_BYTES, "upload.too_large"),
    ],
)
def test_bad_images_rejected(data: bytes, key: str) -> None:
    with pytest.raises(UploadRejected) as caught:
        check_image(data)
    assert caught.value.key == key


@pytest.mark.parametrize(
    ("data", "mime"),
    [
        (b"OggS" + b"\0" * 40, "audio/ogg"),
        (b"\x1a\x45\xdf\xa3" + b"\0" * 40, "audio/webm"),
        (b"RIFF\0\0\0\0WAVEfmt " + b"\0" * 30, "audio/wav"),
        (b"ID3" + b"\0" * 40, "audio/mpeg"),
        (b"\0\0\0\x20ftypM4A " + b"\0" * 30, "audio/mp4"),
    ],
)
def test_audio_types(data: bytes, mime: str) -> None:
    assert check_audio(data).mime == mime


def test_audio_rejects_other_files() -> None:
    with pytest.raises(UploadRejected):
        check_audio(b"#!/bin/sh\nrm -rf /")
