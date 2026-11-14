"""Owner-facing text in English, Hindi and Marathi, loaded from app/i18n/<lang>.json."""

import json
import logging
from functools import lru_cache
from pathlib import Path
from typing import Literal

from starlette.requests import Request

Language = Literal["en", "hi", "mr"]
SUPPORTED: tuple[Language, ...] = ("en", "hi", "mr")
DEFAULT_LANGUAGE: Language = "en"
I18N_DIR = Path(__file__).resolve().parent.parent / "i18n"

logger = logging.getLogger(__name__)


@lru_cache
def load_strings(lang: str) -> dict[str, str]:
    """Load and cache the flat key -> text map for one language."""
    path = I18N_DIR / f"{lang}.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    return {str(k): str(v) for k, v in data.items()}


def normalise_language(value: str | None) -> Language | None:
    """Return a supported language code for values like "hi", "hi-IN" or "MR", else None."""
    if not value:
        return None
    code = value.strip().lower().split("-")[0].split("_")[0]
    for supported in SUPPORTED:
        if code == supported:
            return supported
    return None


def t(key: str, lang: str = DEFAULT_LANGUAGE, **values: object) -> str:
    """Return the text for key in lang, falling back to English, then to the key itself."""
    code = normalise_language(lang) or DEFAULT_LANGUAGE
    text = load_strings(code).get(key)
    if text is None:
        logger.warning("missing_i18n_key", extra={"key": key, "lang": code})
        text = load_strings(DEFAULT_LANGUAGE).get(key, key)
    return text.format(**values) if values else text


def pick_language(request: Request) -> Language:
    """Choose the owner's language: ?lang=, then the session, then Accept-Language, then English."""
    from_query = normalise_language(request.query_params.get("lang"))
    if from_query:
        return from_query
    if "session" in request.scope:
        from_session = normalise_language(request.session.get("lang"))
        if from_session:
            return from_session
    for part in request.headers.get("accept-language", "").split(","):
        lang = normalise_language(part.split(";")[0])
        if lang:
            return lang
    return DEFAULT_LANGUAGE
