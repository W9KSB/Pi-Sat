"""Persistence for the newest received APRS packets."""
from __future__ import annotations

import json
import os
from pathlib import Path
from threading import Lock
from typing import Any


class AprsLog:
    """Keeps the newest packets, newest first, in one atomically written file."""

    def __init__(self, root: Path, retention: int = 25):
        self.root = Path(root)
        self.retention = max(1, int(retention))
        self._path = self.root / "packets.json"
        self._lock = Lock()
        self.root.mkdir(parents=True, exist_ok=True)

    def list(self) -> list[dict[str, Any]]:
        with self._lock:
            return self._read_locked()

    def append(self, entry: dict[str, Any]) -> list[dict[str, Any]]:
        with self._lock:
            entries = [entry, *self._read_locked()][: self.retention]
            self._atomic_write(json.dumps(entries, indent=2, sort_keys=True).encode("utf-8"))
            return entries

    def clear(self) -> list[dict[str, Any]]:
        with self._lock:
            self._atomic_write(b"[]")
            return []

    def _read_locked(self) -> list[dict[str, Any]]:
        try:
            payload = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            return []
        if not isinstance(payload, list):
            return []
        entries = [
            item
            for item in payload
            if isinstance(item, dict) and isinstance(item.get("id"), str)
        ]
        return entries[: self.retention]

    def _atomic_write(self, data: bytes) -> None:
        temporary = self._path.with_suffix(self._path.suffix + ".tmp")
        try:
            with temporary.open("wb") as output:
                output.write(data)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, self._path)
        finally:
            temporary.unlink(missing_ok=True)
