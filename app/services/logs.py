"""Structured JSON logging with request IDs and redaction of secrets and phone numbers.

Never pass audio, uploaded file bytes or full profiles to the logger.
"""

import json
import logging
import re
from contextvars import ContextVar
from datetime import UTC, datetime
from typing import Any

request_id_var: ContextVar[str] = ContextVar("request_id", default="-")

REDACTED = "[REDACTED]"
_SENSITIVE_KEY = re.compile(r"(password|secret|token|api_?key|pin|authorization|cookie)", re.I)
_TOKEN_PATTERNS = [
    re.compile(r"sk-ant-[A-Za-z0-9_\-]{8,}"),
    re.compile(r"(?i)bearer\s+[A-Za-z0-9._\-]{8,}"),
]
_PHONE_PATTERNS = [
    re.compile(r"(?<![\w+])(?:\+?91[\s\-]?)?[6-9]\d{4}[\s\-]?\d{5}(?!\w)"),
    re.compile(r"(?<![\w+])\+\d[\d\s\-]{8,16}\d(?!\w)"),
]
_STANDARD_ATTRS = frozenset(
    vars(logging.LogRecord("", 0, "", 0, "", None, None)).keys() | {"message", "asctime"}
)
_UNREDACTED_FIELDS = frozenset({"ts", "level", "logger", "request_id"})


def mask_phone(match: re.Match[str]) -> str:
    """Replace a phone number with stars, keeping only the last 4 digits."""
    digits = re.sub(r"\D", "", match.group(0))
    return "******" + digits[-4:]


class Redactor:
    """Removes known secret values, token-like strings and full phone numbers from text."""

    def __init__(self, secret_values: list[str] | None = None) -> None:
        self._secrets = sorted(set(secret_values or []), key=len, reverse=True)

    def text(self, value: str) -> str:
        """Redact one string."""
        for secret in self._secrets:
            value = value.replace(secret, REDACTED)
        for pattern in _TOKEN_PATTERNS:
            value = pattern.sub(REDACTED, value)
        for pattern in _PHONE_PATTERNS:
            value = pattern.sub(mask_phone, value)
        return value

    def value(self, key: str, value: Any) -> Any:
        """Redact a structured value; keys that look sensitive are blanked entirely."""
        if _SENSITIVE_KEY.search(key):
            return REDACTED
        if isinstance(value, str):
            return self.text(value)
        if isinstance(value, dict):
            return {k: self.value(str(k), v) for k, v in value.items()}
        if isinstance(value, list | tuple):
            return [self.value(key, v) for v in value]
        if isinstance(value, bytes | bytearray):
            return f"<{len(value)} bytes>"
        return value


class JsonFormatter(logging.Formatter):
    """Formats each record as one JSON line, redacting everything except fixed fields."""

    def __init__(self, redactor: Redactor) -> None:
        super().__init__()
        self._redactor = redactor

    def format(self, record: logging.LogRecord) -> str:
        """Return the record as a redacted JSON string."""
        entry: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "request_id": request_id_var.get(),
            "msg": record.getMessage(),
        }
        for key, value in vars(record).items():
            if key not in _STANDARD_ATTRS and not key.startswith("_"):
                entry[key] = value
        if record.exc_info:
            entry["exc"] = self.formatException(record.exc_info)
        safe = {
            k: (v if k in _UNREDACTED_FIELDS else self._redactor.value(k, v))
            for k, v in entry.items()
        }
        return json.dumps(safe, ensure_ascii=False, default=str)


def setup_logging(secret_values: list[str], level: str = "INFO") -> None:
    """Send all logs (app and uvicorn) to stdout as redacted JSON lines."""
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter(Redactor(secret_values)))
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level)
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        uv_logger = logging.getLogger(name)
        uv_logger.handlers = []
        uv_logger.propagate = True
    logging.getLogger("httpx").setLevel(logging.WARNING)
    # Our request log records the route pattern only; uvicorn's access log would print full
    # URLs with query strings and private tokens, so it stays off.
    logging.getLogger("uvicorn.access").disabled = True
