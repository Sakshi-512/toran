"""The Docker image carries the eval summary the home page reads, and nothing else from evals/."""

import fnmatch
import re
from pathlib import Path, PurePosixPath

from app.services import showcase

ROOT = Path(__file__).resolve().parents[1]
APP_ROOT_IN_IMAGE = PurePosixPath("/app")  # WORKDIR in the Dockerfile


def _copies() -> list[tuple[str, str]]:
    text = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert re.search(r"^WORKDIR /app$", text, re.M)
    return re.findall(r"^COPY (\S+) (\S+)$", text, re.M)


def _in_build_context(path: str) -> bool:
    """Apply .dockerignore the way Docker does: the last matching pattern wins."""
    included = True
    for line in (ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        negate = line.startswith("!")
        pattern = line.lstrip("!")
        parts = PurePosixPath(path).parts
        prefixes = ["/".join(parts[: i + 1]) for i in range(len(parts))]
        if any(fnmatch.fnmatch(p, pattern) for p in prefixes):
            included = negate
    return included


def _in_image(relative: str) -> PurePosixPath:
    """Where the loader's rule (two folders above app/services/) points inside the image."""
    showcase_file = APP_ROOT_IN_IMAGE / "app" / "services" / "showcase.py"
    return showcase_file.parents[2] / relative


def test_dockerfile_copies_only_the_eval_summary() -> None:
    copies = _copies()
    assert ("evals/latest.json", "./evals/latest.json") in copies
    assert [c for c in copies if c[0].startswith("evals")] == [
        ("evals/latest.json", "./evals/latest.json")
    ]


def test_dockerignore_lets_only_the_summary_through() -> None:
    assert _in_build_context("evals/latest.json")
    for private in ("evals/reports/eval-20261119-043858.md", "evals/cases/sample-en.json"):
        assert not _in_build_context(private)


def test_loader_path_matches_the_copied_file() -> None:
    relative = showcase.EVAL_SUMMARY.relative_to(ROOT).as_posix()
    assert relative == "evals/latest.json"
    assert _in_image(relative) == PurePosixPath("/app/evals/latest.json")
    assert _in_image(relative) == APP_ROOT_IN_IMAGE / "evals/latest.json"
