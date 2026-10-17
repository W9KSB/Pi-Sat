"""Bounded reader for the documented WSJT-X UDP wire format."""

from __future__ import annotations

import math
import struct
from typing import Any

MAGIC = 0xADBCCBDA
MAX_PACKET_BYTES = 65507
MAX_STRING_BYTES = 16384


class ProtocolError(ValueError):
    """The datagram does not contain a supported, well-formed message."""


def _command_header(kind: int, client_id: str, schema: int) -> bytes:
    if schema not in (2, 3) or not isinstance(client_id, str) or not client_id:
        raise ProtocolError("Invalid command schema or client id")
    return struct.pack(">III", MAGIC, schema, kind) + _write_string(client_id)


def _write_string(value: str) -> bytes:
    if not isinstance(value, str):
        raise ProtocolError("Command string must be text")
    raw = value.encode("utf-8")
    if len(raw) > MAX_STRING_BYTES:
        raise ProtocolError("Command string is too long")
    return struct.pack(">I", len(raw)) + raw


def build_halt_tx(client_id: str, *, schema: int = 3) -> bytes:
    """Request immediate halt, including an in-progress transmission."""
    return _command_header(8, client_id, schema) + b"\x00"


def build_replay(client_id: str, *, schema: int = 3) -> bytes:
    """Request existing decodes followed by a fresh engine status."""
    return _command_header(7, client_id, schema)


def build_free_text(client_id: str, message: str, *, schema: int = 3) -> bytes:
    """Populate and select Tx5; this does not enable automatic transmission."""
    if not message or len(message) > 40 or any(character not in "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789 /+-<>" for character in message):
        raise ProtocolError("Invalid FT4/FT8 message")
    return _command_header(9, client_id, schema) + _write_string(message) + b"\x01"


def build_configure_rx_df(client_id: str, frequency_hz: int, *, schema: int = 3) -> bytes:
    """Set only RX audio frequency while preserving mode and station settings."""
    if type(frequency_hz) is not int or not 0 <= frequency_hz <= 5000:
        raise ProtocolError("RX audio frequency must be between 0 and 5000 Hz")
    payload = (_write_string("") + struct.pack(">I", 0xFFFFFFFF) + _write_string("")
               + b"\x00" + struct.pack(">II", 0xFFFFFFFF, frequency_hz)
               + _write_string("") + _write_string("") + b"\x00")
    return _command_header(15, client_id, schema) + payload


class _Reader:
    def __init__(self, data: bytes) -> None:
        self.data = data
        self.offset = 0

    @property
    def remaining(self) -> int:
        return len(self.data) - self.offset

    def number(self, fmt: str) -> Any:
        size = struct.calcsize(">" + fmt)
        if self.remaining < size:
            raise ProtocolError("Truncated field")
        result = struct.unpack_from(">" + fmt, self.data, self.offset)[0]
        self.offset += size
        return result

    def string(self) -> str:
        size = self.number("I")
        if size == 0xFFFFFFFF:
            return ""
        if size > MAX_STRING_BYTES or size > self.remaining:
            raise ProtocolError("Invalid string length")
        raw = self.data[self.offset:self.offset + size]
        self.offset += size
        try:
            return raw.decode("utf-8", errors="strict")
        except UnicodeDecodeError as exc:
            raise ProtocolError("Invalid UTF-8") from exc

    def boolean(self) -> bool:
        value = self.number("B")
        if value not in (0, 1):
            raise ProtocolError("Invalid boolean")
        return bool(value)


def parse_datagram(data: bytes) -> dict[str, Any] | None:
    """Read supported messages; ignore unknown types and future trailing fields."""
    if not 12 <= len(data) <= MAX_PACKET_BYTES:
        raise ProtocolError("Invalid datagram length")
    reader = _Reader(data)
    if reader.number("I") != MAGIC:
        raise ProtocolError("Invalid magic number")
    schema = reader.number("I")
    if schema not in (2, 3):
        raise ProtocolError("Unsupported schema")
    kind = reader.number("I")
    if kind not in (0, 1, 2, 3, 6):
        return None
    result: dict[str, Any] = {"type": kind, "schema": schema, "id": reader.string()}
    if kind == 0:
        result["maximum_schema"] = reader.number("I") if reader.remaining else 2
        if schema == 3 and result["maximum_schema"] < 3:
            raise ProtocolError("Missing or invalid maximum schema")
        for field in ("version", "revision"):
            result[field] = reader.string() if reader.remaining else ""
    elif kind == 1:
        fields = (
            ("dial_frequency_hz", "Q"), ("mode", "string"),
            ("dx_call", "string"), ("report", "string"), ("tx_mode", "string"),
            ("tx_enabled", "boolean"), ("transmitting", "boolean"),
            ("decoding", "boolean"), ("rx_df_hz", "I"), ("tx_df_hz", "I"),
            ("de_call", "string"), ("de_grid", "string"), ("dx_grid", "string"),
        )
        status: dict[str, Any] = {}
        optional = (
            ("tx_watchdog", "boolean"), ("sub_mode", "string"),
            ("fast_mode", "boolean"), ("special_operation_mode", "B"),
            ("frequency_tolerance_hz", "I"), ("tr_period_seconds", "I"),
            ("configuration_name", "string"), ("tx_message", "string"),
        )
        for field, fmt in fields + optional:
            if (field, fmt) in optional and not reader.remaining:
                break
            status[field] = getattr(reader, fmt)() if fmt in ("string", "boolean") else reader.number(fmt)
        result["status"] = status
    elif kind == 2:
        result["new"] = reader.boolean()
        result["time_ms"] = reader.number("I")
        if result["time_ms"] >= 86400000:
            raise ProtocolError("Invalid QTime")
        result["snr_db"] = reader.number("i")
        result["dt_seconds"] = reader.number("d")
        if not math.isfinite(result["dt_seconds"]):
            raise ProtocolError("Invalid delta time")
        result["frequency_hz"] = reader.number("I")
        mode = reader.string()
        result["mode"] = {"~": "FT8", "+": "FT4"}.get(mode, mode.upper())
        result["message"] = reader.string()
        result["low_confidence"] = reader.boolean() if reader.remaining else False
        result["off_air"] = reader.boolean() if reader.remaining else False
    return result
