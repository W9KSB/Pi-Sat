"""Atomic local contact history and ADIF export."""

from __future__ import annotations

import copy
import json
import os
import tempfile
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def utc_time(value: str) -> datetime:
    instant = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if instant.tzinfo is None:
        raise ValueError("Contact times must include a UTC offset")
    return instant.astimezone(timezone.utc)


class WsjtxContactStore:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._lock = threading.RLock()
        self._contacts: list[dict[str, Any]] = []
        self.error = ""
        try:
            if self.path.exists():
                document = json.loads(self.path.read_text(encoding="utf-8"))
                if not isinstance(document, dict) or document.get("version") != 1 or not isinstance(document.get("contacts"), list):
                    raise ValueError("Invalid WSJT-X contact history")
                for contact in document["contacts"]:
                    self._validate(contact)
                self._contacts = document["contacts"]
        except (OSError, ValueError, TypeError) as error:
            self.error = f"Contact history is unavailable; preserve and repair {self.path.name}: {error}"

    @staticmethod
    def _validate(contact: dict[str, Any]) -> None:
        if not isinstance(contact, dict):
            raise ValueError("Invalid WSJT-X contact")
        for field in ("id", "time_on_utc", "time_off_utc", "call", "grid", "mode", "report_sent", "report_received", "satellite"):
            value = contact.get(field)
            if not isinstance(value, str) or any(ord(char) < 32 or ord(char) > 126 for char in value):
                raise ValueError(f"Contact {field} must contain printable ASCII text")
        if not contact["id"] or not contact["call"] or contact["mode"] not in ("FT4", "FT8"):
            raise ValueError("Contact identity and mode are required")
        if utc_time(contact["time_off_utc"]) < utc_time(contact["time_on_utc"]):
            raise ValueError("Contact end precedes its start")
        for field in ("rx_frequency_hz", "tx_frequency_hz"):
            if type(contact.get(field)) is not int or contact[field] <= 0:
                raise ValueError(f"Contact {field} must be a positive integer")

    def list(self) -> list[dict[str, Any]]:
        with self._lock:
            return copy.deepcopy(list(reversed(self._contacts)))

    def append(self, contact: dict[str, Any]) -> dict[str, Any]:
        if self.error:
            raise RuntimeError(self.error)
        record = copy.deepcopy(contact)
        record.setdefault("id", uuid.uuid4().hex)
        self._validate(record)
        with self._lock:
            for existing in self._contacts:
                if existing["id"] == record["id"]:
                    return copy.deepcopy(existing)
            updated = [*self._contacts, record]
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary: str | None = None
            try:
                with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", prefix=".contacts-", suffix=".tmp", dir=self.path.parent, delete=False) as stream:
                    temporary = stream.name
                    json.dump({"version": 1, "contacts": updated}, stream, ensure_ascii=True, separators=(",", ":"))
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, self.path)
                temporary = None
                self._contacts = updated
            finally:
                if temporary is not None:
                    Path(temporary).unlink(missing_ok=True)
            return copy.deepcopy(record)

    def export_adif(self) -> str:
        if self.error:
            raise RuntimeError(self.error)
        def field(name: str, value: str) -> str:
            return f"<{name}:{len(value)}>{value}"

        records = [field("ADIF_VER", "3.1.4") + field("PROGRAMID", "Pi-Sat") + "<EOH>"]
        for contact in reversed(self.list()):
            started, ended = utc_time(contact["time_on_utc"]), utc_time(contact["time_off_utc"])
            values = {
                "CALL": contact["call"], "GRIDSQUARE": contact["grid"], "MODE": contact["mode"],
                "QSO_DATE": started.strftime("%Y%m%d"), "TIME_ON": started.strftime("%H%M%S"),
                "QSO_DATE_OFF": ended.strftime("%Y%m%d"), "TIME_OFF": ended.strftime("%H%M%S"),
                "RST_SENT": contact["report_sent"], "RST_RCVD": contact["report_received"],
                "FREQ": f"{contact['tx_frequency_hz'] // 1000000}.{contact['tx_frequency_hz'] % 1000000:06d}",
                "FREQ_RX": f"{contact['rx_frequency_hz'] // 1000000}.{contact['rx_frequency_hz'] % 1000000:06d}",
            }
            if contact["satellite"]:
                values.update(SAT_NAME=contact["satellite"], PROP_MODE="SAT")
            records.append("".join(field(name, value) for name, value in values.items() if value) + "<EOR>")
        return "\n".join(records) + "\n"
