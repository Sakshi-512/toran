"""Readable web addresses (subdomains) from business names, including names in Devanagari."""

import re
import unicodedata

from sqlmodel import Session, select

from app.models import Business
from app.services.sites import RESERVED_LABELS

MIN_LENGTH = 3
MAX_LENGTH = 40
_VALID = re.compile(r"^[a-z0-9](?:[a-z0-9-]*[a-z0-9])?$")

# Common words in shop names, written in Devanagari, with their usual English spelling.
LOANWORDS = {
    "ब्युटी": "beauty",
    "ब्यूटी": "beauty",
    "पार्लर": "parlour",
    "बेकरी": "bakery",
    "केक": "cake",
    "केक्स": "cakes",
    "होम": "home",
    "टेलर": "tailor",
    "टेलर्स": "tailors",
    "क्लासेस": "classes",
    "क्लास": "class",
    "ट्यूशन": "tuition",
    "ट्युशन": "tuition",
    "मेहंदी": "mehendi",
    "मेहेंदी": "mehendi",
    "फूड्स": "foods",
    "फूड": "food",
    "किचन": "kitchen",
    "स्टुडिओ": "studio",
    "स्टूडियो": "studio",
    "सलून": "salon",
    "बुटीक": "boutique",
    "गृहउद्योग": "gruh-udyog",
    "शॉप": "shop",
    "स्वीट्स": "sweets",
    "स्नॅक्स": "snacks",
    "आर्ट": "art",
    "आर्ट्स": "arts",
    "सेंटर": "centre",
    "कलेक्शन": "collection",
}
_CONSONANTS = {
    "क": "k",
    "ख": "kh",
    "ग": "g",
    "घ": "gh",
    "ङ": "n",
    "च": "ch",
    "छ": "chh",
    "ज": "j",
    "झ": "jh",
    "ञ": "n",
    "ट": "t",
    "ठ": "th",
    "ड": "d",
    "ढ": "dh",
    "ण": "n",
    "त": "t",
    "थ": "th",
    "द": "d",
    "ध": "dh",
    "न": "n",
    "प": "p",
    "फ": "ph",
    "ब": "b",
    "भ": "bh",
    "म": "m",
    "य": "y",
    "र": "r",
    "ल": "l",
    "ळ": "l",
    "व": "v",
    "श": "sh",
    "ष": "sh",
    "स": "s",
    "ह": "h",
    "क़": "q",
    "ख़": "kh",
    "ग़": "g",
    "ज़": "z",
    "ड़": "r",
    "ढ़": "rh",
    "फ़": "f",
}
_VOWELS = {
    "अ": "a",
    "आ": "a",
    "इ": "i",
    "ई": "i",
    "उ": "u",
    "ऊ": "u",
    "ए": "e",
    "ऐ": "ai",
    "ओ": "o",
    "औ": "au",
    "ऋ": "ru",
    "ॲ": "a",
    "ऑ": "o",
}
_SIGNS = {
    "ा": "a",
    "ि": "i",
    "ी": "i",
    "ु": "u",
    "ू": "u",
    "े": "e",
    "ै": "ai",
    "ो": "o",
    "ौ": "au",
    "ृ": "ru",
    "ॅ": "e",
    "ॉ": "o",
}
_NASAL = {"ं": "n", "ँ": "n", "ः": "h"}
_VIRAMA = "्"


def _word(word: str) -> str:
    """Transliterate one Devanagari word, dropping the final inherent vowel."""
    if word in LOANWORDS:
        return LOANWORDS[word]
    out: list[str] = []
    chars = unicodedata.normalize("NFC", word)
    i = 0
    while i < len(chars):
        char = chars[i]
        nxt = chars[i + 1] if i + 1 < len(chars) else ""
        if char in _CONSONANTS:
            out.append(_CONSONANTS[char])
            if nxt in _SIGNS:
                out.append(_SIGNS[nxt])
                i += 1
            elif nxt == _VIRAMA:
                i += 1
            elif i + 1 < len(chars):
                out.append("a")
        elif char in _VOWELS:
            out.append(_VOWELS[char])
        elif char in _NASAL:
            out.append(_NASAL[char])
        elif char.isascii():
            out.append(char)
        i += 1
    return "".join(out)


def transliterate(text: str) -> str:
    """Readable Latin letters for a name: "सुनीता ब्युटी पार्लर" -> "sunita beauty parlour"."""
    return " ".join(_word(w) for w in re.split(r"[\s:,.&/()\-]+", text) if w)


def clean(value: str) -> str:
    """Lower-case letters, numbers and single hyphens, trimmed to MAX_LENGTH."""
    value = re.sub(r"['’]", "", value)
    value = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode().lower()
    value = re.sub(r"[^a-z0-9]+", "-", value).strip("-")
    return value[:MAX_LENGTH].strip("-")


def problem(value: str) -> str | None:
    """i18n key describing what is wrong with a web address, or None if it can be used."""
    if len(value) < MIN_LENGTH or len(value) > MAX_LENGTH or not _VALID.match(value):
        return "intake.address_invalid"
    if "--" in value or value in RESERVED_LABELS:
        return "intake.address_invalid"
    return None


def is_taken(session: Session, value: str) -> bool:
    """True if another business already uses this web address."""
    return session.exec(select(Business).where(Business.slug == value)).first() is not None


def suggest(session: Session, name: str, suggestion: str | None = None) -> str:
    """A free, readable web address: Claude's suggestion, else the transliterated name."""
    for candidate in (clean(suggestion or ""), clean(transliterate(name))):
        if problem(candidate) is None:
            return _free(session, candidate)
    return _free(session, "my-business")


def _free(session: Session, base: str) -> str:
    if not is_taken(session, base):
        return base
    for n in range(2, 100):
        candidate = f"{base[: MAX_LENGTH - 3]}-{n}"
        if not is_taken(session, candidate):
            return candidate
    raise RuntimeError("no free web address")  # pragma: no cover
