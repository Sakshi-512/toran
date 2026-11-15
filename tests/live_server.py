"""Run the real app on a free local port in a background thread (for browser tests)."""

import socket
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager

import uvicorn

from app.main import create_app


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@contextmanager
def live_server() -> Iterator[int]:
    """Serve the app (without its background worker) and yield the port."""
    port = _free_port()
    config = uvicorn.Config(
        create_app(),
        host="127.0.0.1",
        port=port,
        lifespan="off",
        log_config=None,
        loop="asyncio",
        access_log=False,
    )
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started:
        if time.monotonic() > deadline:
            raise RuntimeError("live server did not start")
        time.sleep(0.05)
    try:
        yield port
    finally:
        server.should_exit = True
        thread.join(timeout=10)
