"""Tiny health endpoint for container orchestration."""

from __future__ import annotations

import json
import logging
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable

log = logging.getLogger(__name__)


class _Handler(BaseHTTPRequestHandler):
    state_fn: Callable[[], dict]

    def do_GET(self) -> None:  # noqa: N802 (stdlib naming)
        if self.path not in ("/healthz", "/"):
            self.send_response(404)
            self.end_headers()
            return
        try:
            body = json.dumps(self.state_fn(), default=str).encode("utf-8")
            self.send_response(200)
        except Exception:
            body = b'{"ok": false}'
            self.send_response(500)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt: str, *args) -> None:  # silence access logs
        log.debug("health: " + fmt, *args)


class HealthServer:
    def __init__(self, port: int, state_fn: Callable[[], dict]) -> None:
        self.port = port
        self.state_fn = state_fn
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        handler = type("BoundHandler", (_Handler,), {"state_fn": staticmethod(self.state_fn)})
        try:
            self._server = ThreadingHTTPServer(("0.0.0.0", self.port), handler)
        except OSError as exc:
            log.warning("health server disabled on port %s: %s", self.port, exc)
            return
        self._thread = threading.Thread(target=self._server.serve_forever, name="health", daemon=True)
        self._thread.start()
        log.info("health endpoint listening on :%d/healthz", self.port)

    def stop(self) -> None:
        if self._server:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
