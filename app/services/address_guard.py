"""Detects text that looks like a home address, so it is never shown on a site (rule 3).

Area and locality names (Kothrud, Sinhagad Road, FC Road) pass. House, flat and plot numbers,
building or society names, lane numbers and PIN codes are flagged.
"""

import re

_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("pin_code", re.compile(r"(?<!\d)[1-9]\d{2}\s?\d{3}(?!\d)")),
    (
        "building_word",
        re.compile(
            r"\b(flat|house|plot|shop|room|bungalow|bldg|building|apartments?|apt|wing|floor|"
            r"society|soc|chawl|tower|block|h\.?\s?no|door\s?no)\b",
            re.IGNORECASE,
        ),
    ),
    ("lane_number", re.compile(r"\b(lane|gali|galli)\s*(no\.?\s*)?\d+", re.IGNORECASE)),
    (
        "unit_number",
        re.compile(r"(?<![\w-])[A-Za-z]?-?\d{1,4}\s*/\s*\d{1,4}|#\s?\d+|\b[A-Za-z]-\d{2,4}\b"),
    ),
    ("leading_number", re.compile(r"^\s*\d+[A-Za-z]?\s*,")),
    (
        "building_word_devanagari",
        re.compile(r"(फ्लॅट|फ्लैट|सदनिका|मकान\s?नं|घर\s?(नं|क्रमांक)|सोसायटी|इमारत|बिल्डिंग|प्लॉट|मजला|मंज़िल)"),
    ),
)


def address_reason(text: str) -> str | None:
    """Return why text looks like an address (a short code), or None if it looks like an area."""
    for reason, pattern in _PATTERNS:
        if pattern.search(text or ""):
            return reason
    return None


def looks_like_address(text: str) -> bool:
    """True if text looks like a home address rather than an area name."""
    return address_reason(text) is not None
