"""Field rules shared by the server checks and the form markup, so the two cannot drift.

The server is the only authority: these rules run in the confirm check, and the same limits
are rendered into the forms as maxlength and other input hints.
"""

import unicodedata

from app.schemas import MAX_PRICE

NAME_MIN = 2
NAME_MAX = 120
# Punctuation a business name may use, besides letters, digits and spaces.
NAME_PUNCTUATION = frozenset("&'\u2019-.,()/")  # \u2019 is the curly apostrophe
# Zero-width joiners shape Devanagari conjuncts (for example the Marathi eyelash ra).
_JOINERS = frozenset("‌‍")

PRICE_MAX = MAX_PRICE


def business_name_problem(name: str) -> str | None:
    """i18n key saying what is wrong with a business name, or None if it can be used.

    Letters in any script (with their combining marks), digits, spaces and NAME_PUNCTUATION
    are allowed; at least one letter is required; control characters are refused.
    """
    if not NAME_MIN <= len(name) <= NAME_MAX:
        return "intake.name_length"
    has_letter = False
    for char in name:
        category = unicodedata.category(char)
        if category[0] == "L":
            has_letter = True
        elif category[0] == "M" or category == "Nd" or char == " " or char in _JOINERS:
            continue
        elif char not in NAME_PUNCTUATION:
            return "intake.name_chars"
    return None if has_letter else "intake.name_letter"
