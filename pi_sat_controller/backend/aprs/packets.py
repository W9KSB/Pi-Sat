"""Interpretation of APRS information fields for the receive-only module."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from pi_sat_controller.backend.aprs.ax25 import Ax25Frame


_DATA_TYPES = {
    "!": "Position",
    "=": "Position",
    "/": "Position",
    "@": "Position",
    ":": "Message",
    ";": "Object",
    ")": "Item",
    ">": "Status",
    "T": "Telemetry",
    "_": "Weather",
}
_TIMESTAMPED_POSITIONS = frozenset({"/", "@"})
_POSITION_PREFIX_LENGTH = 19
_SUMMARY_LIMIT = 200


def clean_text(value: str) -> str:
    """Keep a display-safe ASCII subset so control bytes never reach the UI."""
    return "".join(
        character if 0x20 <= ord(character) < 0x7F else " " for character in value
    )


def describe(frame: Ax25Frame) -> dict[str, Any] | None:
    """Build one APRS log entry, or None when there is no information field."""
    if not frame.info:
        return None
    info = clean_text(frame.info.decode("latin-1", errors="replace")).rstrip()
    data_type = info[0] if info else " "
    kind = _DATA_TYPES.get(data_type, "Other")
    latitude: float | None = None
    longitude: float | None = None
    addressee: str | None = None

    if data_type in "!=/@":
        body = info[8:] if data_type in _TIMESTAMPED_POSITIONS else info[1:]
        latitude, longitude, detail = _decode_position(body)
        if latitude is None:
            summary = detail or "Position"
        else:
            summary = f"Position {latitude:.5f}, {longitude:.5f}"
            if detail:
                summary = f"{summary} - {detail}"
    elif data_type == ":":
        addressee = info[1:10].strip() or None
        body = _strip_message_id(info[10:])
        summary = f"Message to {addressee}: {body}" if addressee else f"Message: {body}"
    elif data_type == ">":
        summary = f"Status: {info[1:].strip()}" if info[1:].strip() else "Status"
    elif data_type == ";":
        summary = f"Object {info[1:10].strip() or 'unnamed'}"
    elif data_type == ")":
        summary = f"Item {info[1:4].strip() or 'unnamed'}"
    elif data_type == "T":
        summary = f"Telemetry {info[1:].strip()}"
    elif data_type == "_":
        summary = f"Weather {info[1:].strip()}"
    else:
        summary = f"Unrecognized APRS data ({data_type.strip() or 'empty'})"

    now = datetime.now(timezone.utc)
    return {
        "schema_version": 1,
        "id": uuid4().hex,
        "timestamp_utc": now.isoformat(),
        "source": frame.source,
        "destination": frame.destination,
        "path": list(frame.path),
        "kind": kind,
        "data_type": data_type,
        "summary": clean_text(summary)[:_SUMMARY_LIMIT].strip(),
        "info": info,
        "raw": clean_text(frame.tnc2())[:_SUMMARY_LIMIT * 2],
        "latitude": latitude,
        "longitude": longitude,
        "addressee": addressee,
    }


def _strip_message_id(body: str) -> str:
    end = body.find("{")
    return body[:end].strip() if end >= 0 else body.strip()


def _decode_position(body: str) -> tuple[float | None, float | None, str]:
    """Decode an uncompressed APRS position, returning (lat, lon, detail)."""
    if not body:
        return None, None, ""
    if not body[0].isdigit():
        # Compressed positions use a base-91 encoding; they are reported as
        # undecoded rather than guessed at.
        return None, None, "Compressed position"
    if len(body) < _POSITION_PREFIX_LENGTH:
        return None, None, ""
    try:
        latitude = int(body[0:2]) + float(body[2:7]) / 60.0
        longitude = int(body[9:12]) + float(body[12:17]) / 60.0
    except ValueError:
        return None, None, ""
    if body[7].upper() == "S":
        latitude = -latitude
    if body[17].upper() == "W":
        longitude = -longitude
    if not -90.0 <= latitude <= 90.0 or not -180.0 <= longitude <= 180.0:
        return None, None, ""
    return latitude, longitude, body[_POSITION_PREFIX_LENGTH:].strip()
