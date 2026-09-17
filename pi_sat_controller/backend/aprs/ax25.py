"""KISS framing and AX.25 UI-frame decoding for the APRS module."""
from __future__ import annotations

from dataclasses import dataclass


KISS_FEND = 0xC0
KISS_FESC = 0xDB
KISS_TFEND = 0xDC
KISS_TFESC = 0xDD

_KISS_DATA_COMMAND = 0x00
_MAX_AX25_ADDRESSES = 8
_ADDRESS_LENGTH = 7
_UI_CONTROL_MASK = 0xEF
_UI_CONTROL = 0x03


@dataclass(frozen=True)
class Ax25Frame:
    destination: str
    source: str
    path: tuple[str, ...]
    control: int
    pid: int | None
    info: bytes

    def tnc2(self) -> str:
        """Render the conventional TNC2 monitor form for display and storage."""
        header = f"{self.source}>{self.destination}"
        if self.path:
            header = f"{header},{','.join(self.path)}"
        body = self.info.decode("latin-1", errors="replace")
        return f"{header}:{body}"


class KissDecoder:
    """Reassembles KISS frames from a raw byte stream."""

    def __init__(self) -> None:
        self._buffer = bytearray()
        self._escaped = False

    def feed(self, data: bytes) -> list[bytes]:
        frames: list[bytes] = []
        for value in data:
            if value == KISS_FEND:
                if self._buffer:
                    frames.append(bytes(self._buffer))
                    self._buffer.clear()
                self._escaped = False
                continue
            if self._escaped:
                self._escaped = False
                if value == KISS_TFEND:
                    self._buffer.append(KISS_FEND)
                elif value == KISS_TFESC:
                    self._buffer.append(KISS_FESC)
                continue
            if value == KISS_FESC:
                self._escaped = True
                continue
            self._buffer.append(value)
        return frames


def decode_ax25(frame: bytes) -> Ax25Frame | None:
    """Decode one KISS data frame holding an AX.25 UI frame.

    Anything that is not a well-formed unnumbered-information frame is
    rejected so the caller only ever sees APRS-usable traffic.
    """
    if len(frame) < 2 or frame[0] & 0x0F != _KISS_DATA_COMMAND:
        return None
    return _decode_ui_payload(frame[1:])


def _decode_ui_payload(payload: bytes) -> Ax25Frame | None:
    addresses: list[str] = []
    offset = 0
    while offset + _ADDRESS_LENGTH <= len(payload) and len(addresses) < _MAX_AX25_ADDRESSES:
        field = payload[offset : offset + _ADDRESS_LENGTH]
        callsign = "".join(chr(byte >> 1) for byte in field[:6]).strip()
        if not callsign or any(not 0x20 <= ord(character) < 0x7F for character in callsign):
            return None
        ssid = (field[6] >> 1) & 0x0F
        addresses.append(f"{callsign}-{ssid}" if ssid else callsign)
        offset += _ADDRESS_LENGTH
        if field[6] & 0x01:
            break
    else:
        return None

    if len(addresses) < 2 or offset >= len(payload):
        return None
    control = payload[offset]
    if control & _UI_CONTROL_MASK != _UI_CONTROL:
        return None
    offset += 1
    pid: int | None = None
    if offset < len(payload):
        pid = payload[offset]
        offset += 1
    return Ax25Frame(
        destination=addresses[0],
        source=addresses[1],
        path=tuple(addresses[2:]),
        control=control,
        pid=pid,
        info=payload[offset:],
    )
