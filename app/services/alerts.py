"""Error alert emails: at most one email per error type per hour to CONTACT_TO_EMAIL.

An error type is the logger, the log message and the exception class. The email body is the
already-redacted JSON log line (no secrets, no full phone numbers). Sending happens on a
background thread so logging never blocks. Mailer and alert errors are ignored so alerts can
never loop. The hourly limit is kept in memory, which suits our single app process.
"""

import contextlib
import logging
import queue
import threading
import time
from collections.abc import Callable

from app.config import Settings
from app.services.logs import JsonFormatter, Redactor
from app.services.mailer import send_mail

THROTTLE_SECONDS = 60 * 60
_IGNORED = ("app.services.mailer", "app.services.alerts", "app.services.mail_jobs")


def error_type(record: logging.LogRecord) -> str:
    """Key that groups repeats of the same error."""
    exc = record.exc_info[0].__name__ if record.exc_info and record.exc_info[0] else "-"
    return f"{record.name}:{record.getMessage()}:{exc}"


class AlertHandler(logging.Handler):
    """Queues an alert email for ERROR records, throttled per error type."""

    def __init__(
        self,
        settings: Settings,
        clock: Callable[[], float] = time.monotonic,
        synchronous: bool = False,
    ) -> None:
        super().__init__(level=logging.ERROR)
        self.settings = settings
        self.clock = clock
        self.synchronous = synchronous
        self._redactor = Redactor(settings.secret_values())
        self.setFormatter(JsonFormatter(self._redactor))
        self._last_sent: dict[str, float] = {}
        self._lock = threading.Lock()
        self._queue: queue.SimpleQueue[tuple[str, str]] = queue.SimpleQueue()
        self._thread: threading.Thread | None = None

    def should_send(self, key: str) -> bool:
        """True (and records the time) unless this error type was emailed within the hour."""
        now = self.clock()
        with self._lock:
            last = self._last_sent.get(key)
            if last is not None and now - last < THROTTLE_SECONDS:
                return False
            self._last_sent[key] = now
            return True

    def emit(self, record: logging.LogRecord) -> None:
        """Queue an alert for this record if it is new or the last one was over an hour ago."""
        if record.name.startswith(_IGNORED):
            return
        try:
            key = self._redactor.text(error_type(record))
            if not self.should_send(key):
                return
            item = (key, self.format(record))
            if self.synchronous:
                self._send(*item)
            else:
                self._start_thread()
                self._queue.put(item)
        except Exception:  # never let alerting break the app
            self.handleError(record)

    def _start_thread(self) -> None:
        if self._thread is None or not self._thread.is_alive():
            self._thread = threading.Thread(target=self._drain, name="alerts", daemon=True)
            self._thread.start()

    def _drain(self) -> None:
        while True:
            key, line = self._queue.get()
            self._send(key, line)

    def _send(self, key: str, line: str) -> None:
        if not self.settings.contact_to_email:
            return
        body = (
            f"An error happened in {self.settings.app_name}.\n"
            f"Error type: {key}\n"
            "Further errors of this type are not emailed for an hour.\n\n"
            f"Log entry (secrets and phone numbers removed):\n{line}\n"
        )
        # Alert failures must never raise or loop.
        with contextlib.suppress(Exception):
            send_mail(
                self.settings,
                to=self.settings.contact_to_email,
                subject=f"{self.settings.app_name} error: {key[:80]}",
                body=body,
                kind="alert",
            )


def install_alerts(settings: Settings) -> AlertHandler | None:
    """Attach the alert handler to the root logger when a recipient is configured."""
    if not settings.contact_to_email:
        return None
    root = logging.getLogger()
    for handler in list(root.handlers):
        if isinstance(handler, AlertHandler):
            root.removeHandler(handler)
    handler = AlertHandler(settings)
    root.addHandler(handler)
    return handler
