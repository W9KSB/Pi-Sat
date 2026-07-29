from __future__ import annotations

import re
from typing import Any

import requests

from pi_sat_controller.backend.models import TransponderProfile


DEFAULT_TRANSPONDER_SOURCE_URL = "https://db.satnogs.org/api/transmitters/"
_CTCSS_PATTERN = re.compile(
    r"\bCTCSS(?:\s+TONE)?\s*[:=-]?\s*(\d{2,3}(?:\.\d)?)\s*HZ\b",
    re.IGNORECASE,
)


class TransponderSourceClient:
    def __init__(self, source_url: str = DEFAULT_TRANSPONDER_SOURCE_URL) -> None:
        self.source_url = source_url

    def get_transponders(self, norad_id: int) -> list[TransponderProfile]:
        response = requests.get(
            self.source_url,
            params={"satellite__norad_cat_id": norad_id},
            timeout=15,
        )
        response.raise_for_status()
        transmitters = response.json()
        return [
            transponder
            for transmitter in transmitters
            if (transponder := _transmitter_to_transponder(transmitter)) is not None
        ]


def _transmitter_to_transponder(
    transmitter: dict[str, Any],
) -> TransponderProfile | None:
    if not transmitter.get("downlink_low"):
        return None
    if transmitter.get("alive") is False or transmitter.get("status") != "active":
        return None

    uplink_low = int(transmitter.get("uplink_low") or 0)
    uplink_high = int(transmitter.get("uplink_high") or uplink_low)
    downlink_low = int(transmitter["downlink_low"])
    downlink_high = int(transmitter.get("downlink_high") or downlink_low)
    downlink_mode = str(transmitter.get("mode") or "")
    uplink_mode = str(transmitter.get("uplink_mode") or downlink_mode)
    is_rx_only = uplink_low == 0

    return TransponderProfile(
        name=str(transmitter.get("description") or "Frequency Profile"),
        type=(
            "rx_only"
            if is_rx_only
            else "linear"
            if transmitter.get("type") == "Transponder"
            else "fm"
        ),
        uplink_low=uplink_low,
        uplink_high=uplink_high,
        downlink_low=downlink_low,
        downlink_high=downlink_high,
        uplink_mode=uplink_mode,
        downlink_mode=downlink_mode,
        inverted=bool(transmitter.get("invert")),
        ratio=1.0,
        preferred_uplink=0 if is_rx_only else round((uplink_low + uplink_high) / 2),
        preferred_downlink=round((downlink_low + downlink_high) / 2),
        tone=_extract_ctcss_tone(transmitter),
    )


def _extract_ctcss_tone(transmitter: dict[str, Any]) -> float | None:
    for key in ("ctcss", "tone", "uplink_ctcss", "uplink_tone"):
        raw_value = transmitter.get(key)
        if raw_value in (None, ""):
            continue
        try:
            tone_hz = float(raw_value)
        except (TypeError, ValueError):
            continue
        if 50.0 <= tone_hz <= 300.0:
            return tone_hz
    description = str(transmitter.get("description") or "")
    match = _CTCSS_PATTERN.search(description)
    if match is None:
        return None
    tone_hz = float(match.group(1))
    return tone_hz if 50.0 <= tone_hz <= 300.0 else None
