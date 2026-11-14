import json
import logging
import secrets

from app.services.logs import JsonFormatter, Redactor, request_id_var


def _format(message: str, secrets: list[str] | None = None, **extra: object) -> dict[str, object]:
    record = logging.LogRecord("test", logging.INFO, __file__, 1, message, None, None)
    for key, value in extra.items():
        setattr(record, key, value)
    line = JsonFormatter(Redactor(secrets or [])).format(record)
    return json.loads(line)  # type: ignore[no-any-return]


def test_output_is_json_with_request_id() -> None:
    token = request_id_var.set("abc123")
    try:
        entry = _format("hello")
    finally:
        request_id_var.reset(token)
    assert entry["msg"] == "hello"
    assert entry["request_id"] == "abc123"
    assert entry["level"] == "INFO"


def test_configured_secrets_are_removed() -> None:
    configured = secrets.token_urlsafe(16)
    entry = _format(f"login with {configured} ok", secrets=[configured])
    assert configured not in json.dumps(entry)


def test_api_key_patterns_and_sensitive_fields_removed() -> None:
    fake_key = "sk-ant-api03-" + secrets.token_urlsafe(24)
    entry = _format(
        f"key {fake_key} used",
        password="x",
        pin="1234",
        detail={"api_key": "abc", "ok": 1},
    )
    text = json.dumps(entry)
    assert fake_key not in text
    assert entry["password"] == "[REDACTED]"
    assert entry["pin"] == "[REDACTED]"
    assert entry["detail"] == {"api_key": "[REDACTED]", "ok": 1}


def test_phone_numbers_keep_only_last_four() -> None:
    entry = _format("enquiry from +91 98765 43210 and 9123456789", phone="+919876543210")
    assert "98765" not in json.dumps(entry)
    assert "3210" in str(entry["msg"])
    assert "6789" in str(entry["msg"])
    assert entry["phone"] == "******3210"


def test_dates_and_ids_are_not_mistaken_for_phones() -> None:
    entry = _format("order 2026-11-14 id a98765432101b took 1234 ms")
    assert entry["msg"] == "order 2026-11-14 id a98765432101b took 1234 ms"


def test_bytes_are_never_logged() -> None:
    entry = _format("upload", audio=b"\x00\x01secret-audio")
    assert entry["audio"] == "<14 bytes>"
