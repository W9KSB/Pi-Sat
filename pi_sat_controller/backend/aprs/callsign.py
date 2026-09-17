"""APRS callsign rules shared by the decoder configuration and the transmitter."""
from __future__ import annotations

import os

# Dire Wolf's placeholder callsign. It is fine for a receive-only decoder but
# must never be used on the air.
PLACEHOLDER_MYCALL = "N0CALL"


def resolve_mycall(configured: str | None) -> str:
    """Return the configured callsign, falling back to PI_SAT_APRS_MYCALL."""
    value = str(configured or "").strip()
    if not value:
        value = os.environ.get("PI_SAT_APRS_MYCALL", "").strip()
    return value.upper() or PLACEHOLDER_MYCALL


def is_transmit_callsign(value: str | None) -> bool:
    """True when the value looks like a callsign that may be transmitted.

    This is a sanity gate, not a licence check: it rejects the empty value and
    Dire Wolf's placeholder, and bounds the SSID to the legal 0-15 range.
    """
    text = str(value or "").strip().upper()
    if not text or text == PLACEHOLDER_MYCALL:
        return False
    base, _, ssid = text.partition("-")
    if ssid and not (ssid.isdigit() and 0 <= int(ssid) <= 15):
        return False
    return 3 <= len(base) <= 6 and base.isalnum()
