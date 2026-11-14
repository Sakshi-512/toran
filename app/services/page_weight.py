"""Measures what one generated page actually loads: HTML, CSS, images and the font files whose
unicode-range covers characters on that page (plus the preloaded font, always fetched).
"""

import posixpath
import re
from dataclasses import dataclass, field
from html.parser import HTMLParser

PAGE_WEIGHT_LIMIT = 500 * 1024
_FONT_FACE = re.compile(r"@font-face\s*{(.*?)}", re.S)
_URL = re.compile(r'url\("?([^")]+)"?\)')
_RANGE = re.compile(r"unicode-range:\s*([^;]+);")


@dataclass
class PageWeight:
    """Total bytes for one page and the files that make it up."""

    total: int = 0
    parts: dict[str, int] = field(default_factory=dict)

    def add(self, path: str, size: int) -> None:
        """Count one file once."""
        if path not in self.parts:
            self.parts[path] = size
            self.total += size


class _PageParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.text: list[str] = []
        self.stylesheets: list[str] = []
        self.preloads: list[str] = []
        self.images: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = {k: v or "" for k, v in attrs}
        if tag == "link" and a.get("rel") == "stylesheet":
            self.stylesheets.append(a["href"])
        elif tag == "link" and a.get("rel") == "preload":
            self.preloads.append(a["href"])
        elif tag == "img":
            self.images.append(_largest_candidate(a))

    def handle_data(self, data: str) -> None:
        self.text.append(data)


def _largest_candidate(attrs: dict[str, str]) -> str:
    """The biggest file an <img> may load (worst case: a wide screen)."""
    candidates = []
    for part in attrs.get("srcset", "").split(","):
        bits = part.split()
        if len(bits) == 2 and bits[1].endswith("w"):
            candidates.append((int(bits[1][:-1]), bits[0]))
    return max(candidates)[1] if candidates else attrs["src"]


def _resolve(base_file: str, href: str) -> str:
    return posixpath.normpath(posixpath.join(posixpath.dirname(base_file), href))


def parse_unicode_range(value: str) -> list[tuple[int, int]]:
    """ "U+0000-00FF, U+0131" -> [(0, 255), (305, 305)]."""
    ranges = []
    for item in value.split(","):
        bounds = item.strip().removeprefix("U+").split("-")
        low = int(bounds[0], 16)
        ranges.append((low, int(bounds[-1], 16)))
    return ranges


def _covers(ranges: list[tuple[int, int]], chars: set[int]) -> bool:
    return any(low <= c <= high for c in chars for low, high in ranges)


def page_weight(files: dict[str, bytes], page_path: str) -> PageWeight:
    """Bytes one page loads, with fonts chosen by the characters it shows."""
    weight = PageWeight()
    weight.add(page_path, len(files[page_path]))
    parser = _PageParser()
    parser.feed(files[page_path].decode("utf-8"))
    chars = {ord(c) for c in "".join(parser.text) if not c.isspace()}
    for href in parser.preloads + parser.images:
        path = _resolve(page_path, href)
        weight.add(path, len(files[path]))
    for href in parser.stylesheets:
        css_path = _resolve(page_path, href)
        css = files[css_path].decode("utf-8")
        weight.add(css_path, len(files[css_path]))
        for block in _FONT_FACE.findall(css):
            url, ranges = _URL.search(block), _RANGE.search(block)
            if url and ranges and _covers(parse_unicode_range(ranges.group(1)), chars):
                font_path = _resolve(css_path, url.group(1))
                weight.add(font_path, len(files[font_path]))
    return weight
