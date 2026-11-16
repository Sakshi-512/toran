"""Numbers as owners say or write them: Devanagari digits and Indian comma formats."""

import re

_DEVANAGARI_DIGITS = str.maketrans("०१२३४५६७८९", "0123456789")
_NUMBER = re.compile(r"\d[\d,]*")


def ascii_digits(text: str) -> str:
    """Replace Devanagari digits (०-९) with 0-9; everything else is unchanged."""
    return text.translate(_DEVANAGARI_DIGITS)


def numbers_in(text: str) -> set[str]:
    """Every whole number in text, without grouping commas: "1,500", "1500" and "१५००" -> "1500"."""
    found = set()
    for match in _NUMBER.findall(ascii_digits(text)):
        digits = match.replace(",", "").lstrip("0") or "0"
        found.add(digits)
    return found


def digits_only(text: str) -> str:
    """All digits in text, in order, as ASCII (for phone numbers said with spaces)."""
    return re.sub(r"\D", "", ascii_digits(text))
