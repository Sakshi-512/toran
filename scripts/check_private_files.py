"""Pre-commit hook: refuse to commit real voice notes, photos, private eval cases or data.

The repository is public. Audio is never committed; images only in the folders for site
assets, sample photos, documentation screenshots, test fixtures and fake eval cases.
"""

import sys
from pathlib import PurePosixPath

BLOCKED_DIRS = ("evals/private/", "evals/reports/", "voice_notes/", "data/", "uploads/", "backups/")
AUDIO = {".ogg", ".opus", ".webm", ".m4a", ".mp4", ".wav", ".mp3", ".aac", ".amr", ".3gp", ".flac"}
IMAGES = {".jpg", ".jpeg", ".png", ".webp", ".heic", ".heif", ".gif", ".bmp", ".tiff"}
IMAGE_ALLOWED = (
    "app/static/",
    "app/templates/sites/",
    "docs/screenshots/",
    "tests/fixtures/",
    "evals/cases/files/",
)


def problem(path: str) -> str | None:
    """Why this path must not be committed, or None."""
    posix = PurePosixPath(path).as_posix()
    suffix = PurePosixPath(posix).suffix.lower()
    if posix.startswith(BLOCKED_DIRS):
        return "private folder"
    if suffix in AUDIO:
        return "audio file"
    if suffix in IMAGES and not posix.startswith(IMAGE_ALLOWED):
        return "photo outside the allowed folders"
    return None


def main(paths: list[str]) -> int:
    """Exit 1 and list the files if any must not be committed."""
    bad = [(p, why) for p in paths if (why := problem(p))]
    for path, why in bad:
        print(f"Refusing to commit {path}: {why} (the repository is public).")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
