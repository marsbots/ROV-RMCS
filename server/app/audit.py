"""Append-only audit trail.

Every security- or safety-relevant event is written as one JSON object per line.
In production this should be shipped to durable storage (S3, Loki, BigQuery, ...)
and protected from tampering.

The write is deliberately synchronous: one short line behind a thread lock is
cheap, and doing it inline keeps ordering exact and avoids depending on the
event loop's thread pool during shutdown or cancellation.
"""

from __future__ import annotations

import json
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any


class AuditLog:
    def __init__(self, path: str, memory_limit: int = 500) -> None:
        self.path = Path(path)
        self._lock = threading.Lock()
        self._recent: deque[dict[str, Any]] = deque(maxlen=memory_limit)

    async def start(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.touch(exist_ok=True)

    async def log(self, event: str, **fields: Any) -> dict[str, Any]:
        record = {"ts": time.time(), "event": event, **fields}
        line = json.dumps(record, ensure_ascii=False, default=str)
        with self._lock:
            self._recent.append(record)
            self._append(line)
        return record

    def _append(self, line: str) -> None:
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
            fh.flush()

    def recent(self, limit: int = 200) -> list[dict[str, Any]]:
        if limit <= 0:
            return []
        items = list(self._recent)
        return items[-limit:]
