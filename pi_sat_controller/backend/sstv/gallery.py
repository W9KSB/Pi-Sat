from __future__ import annotations

import binascii
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
from threading import Lock
from typing import Any
from uuid import uuid4
import zlib


_CAPTURE_ID = re.compile(r"^[0-9a-f]{32}$")


def encode_png(width: int, height: int, rgb: bytes) -> bytes:
    """Encode an 8-bit RGB image without adding a runtime image dependency."""
    if not 0 < width <= 2048 or not 0 < height <= 2048:
        raise ValueError("SSTV image dimensions are outside the supported range")
    stride = width * 3
    if len(rgb) != stride * height:
        raise ValueError("SSTV image buffer length does not match its dimensions")

    def chunk(kind: bytes, payload: bytes) -> bytes:
        body = kind + payload
        return (
            len(payload).to_bytes(4, "big")
            + body
            + (binascii.crc32(body) & 0xFFFFFFFF).to_bytes(4, "big")
        )

    scanlines = b"".join(
        b"\x00" + rgb[offset : offset + stride]
        for offset in range(0, len(rgb), stride)
    )
    header = width.to_bytes(4, "big") + height.to_bytes(4, "big") + b"\x08\x02\x00\x00\x00"
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(
        b"IDAT", zlib.compress(scanlines, level=6)
    ) + chunk(b"IEND", b"")


class SstvGallery:
    def __init__(self, root: Path, retention: int = 25):
        self.root = Path(root)
        self.retention = max(1, int(retention))
        self._lock = Lock()
        self.root.mkdir(parents=True, exist_ok=True)

    def list(self) -> list[dict[str, Any]]:
        with self._lock:
            return self._list_locked()

    def _list_locked(self) -> list[dict[str, Any]]:
        captures = []
        for path in self.root.glob("*.json"):
            try:
                metadata = json.loads(path.read_text(encoding="utf-8"))
                capture_id = str(metadata.get("id", ""))
                if not _CAPTURE_ID.fullmatch(capture_id):
                    continue
                if not (self.root / f"{capture_id}.png").is_file():
                    continue
                captures.append(metadata)
            except (OSError, ValueError, TypeError, json.JSONDecodeError):
                continue
        captures.sort(key=lambda item: str(item.get("timestamp_utc", "")), reverse=True)
        return captures

    def save(
        self,
        *,
        mode: str,
        width: int,
        height: int,
        rgb: bytes,
        partial: bool,
        context: dict[str, Any],
    ) -> dict[str, Any]:
        png = encode_png(width, height, rgb)
        capture_id = uuid4().hex
        now = datetime.now(timezone.utc)
        safe_mode = re.sub(r"[^A-Za-z0-9]+", "-", mode).strip("-").lower() or "sstv"
        filename = f"{now:%Y%m%dT%H%M%SZ}_{safe_mode}_{capture_id[:8]}.png"
        metadata = {
            "schema_version": 1,
            "id": capture_id,
            "timestamp_utc": now.isoformat(),
            "mode": mode,
            "width": width,
            "height": height,
            "decode_status": "partial" if partial else "complete",
            "quality": None,
            "frequency_hz": context.get("frequency_hz"),
            "satellite": context.get("satellite"),
            "filename": filename,
        }
        with self._lock:
            self._atomic_write(self.root / f"{capture_id}.png", png)
            try:
                self._atomic_write(
                    self.root / f"{capture_id}.json",
                    json.dumps(metadata, indent=2, sort_keys=True).encode("utf-8"),
                )
            except Exception:
                (self.root / f"{capture_id}.png").unlink(missing_ok=True)
                raise
            captures = self._list_locked()
            for expired in captures[self.retention :]:
                expired_id = str(expired.get("id", ""))
                if _CAPTURE_ID.fullmatch(expired_id):
                    (self.root / f"{expired_id}.png").unlink(missing_ok=True)
                    (self.root / f"{expired_id}.json").unlink(missing_ok=True)
        return metadata

    def find(self, capture_id: str) -> dict[str, Any] | None:
        if not _CAPTURE_ID.fullmatch(capture_id):
            return None
        with self._lock:
            metadata_path = self.root / f"{capture_id}.json"
            image_path = self.root / f"{capture_id}.png"
            try:
                metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            except (OSError, ValueError, TypeError, json.JSONDecodeError):
                return None
            if metadata.get("id") != capture_id or not image_path.is_file():
                return None
            return metadata

    def image_path(self, capture_id: str) -> Path | None:
        if self.find(capture_id) is None:
            return None
        return self.root / f"{capture_id}.png"

    @staticmethod
    def _atomic_write(path: Path, data: bytes) -> None:
        temporary = path.with_suffix(path.suffix + ".tmp")
        try:
            with temporary.open("wb") as output:
                output.write(data)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)
