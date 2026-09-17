"""Sample-level level trim for a module's own copy of the receive audio.

Every consumer of the radio's receive stream is given its own buffer, so a trim
applied here can never change the radio, the browser playback level, or another
module's audio. It is a software control on a secondary stream only.
"""

from __future__ import annotations

import math
import sys
from array import array

RX_GAIN_MIN_DB = -30.0
RX_GAIN_MAX_DB = 12.0
# Dire Wolf and slowrx both decode best with the audio near half scale.
RX_GAIN_DEFAULT_DB = -6.0

_S16_MAX = 32767
_S16_MIN = -32768


def clamp_gain_db(
    value: object,
    *,
    minimum: float = RX_GAIN_MIN_DB,
    maximum: float = RX_GAIN_MAX_DB,
) -> float:
    """Return ``value`` as a decibel trim inside the supported range."""
    try:
        gain = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError) as exc:
        raise ValueError("Level trim must be a number of decibels") from exc
    if not math.isfinite(gain) or not minimum <= gain <= maximum:
        raise ValueError(f"Level trim must be between {minimum:g} and {maximum:g} dB")
    return gain


def apply_gain_db(pcm: bytes, gain_db: float) -> bytes:
    """Return a scaled copy of 16-bit little-endian PCM, clipped to range.

    The input is never modified, so the stream other consumers hold is
    untouched. Clipping rather than wrapping keeps a too-hot trim from turning
    into noise.
    """
    if not pcm or gain_db == 0.0:
        return pcm
    factor = 10.0 ** (gain_db / 20.0)
    usable = len(pcm) - (len(pcm) % 2)
    samples = array("h")
    samples.frombytes(pcm[:usable])
    if sys.byteorder != "little":
        samples.byteswap()
    for index, value in enumerate(samples):
        scaled = int(value * factor)
        samples[index] = _S16_MAX if scaled > _S16_MAX else _S16_MIN if scaled < _S16_MIN else scaled
    if sys.byteorder != "little":
        samples.byteswap()
    return samples.tobytes()


def peak_of(pcm: bytes) -> int:
    """Largest magnitude sample in 16-bit little-endian PCM."""
    usable = len(pcm) - (len(pcm) % 2)
    samples = array("h")
    samples.frombytes(pcm[:usable])
    if sys.byteorder != "little":
        samples.byteswap()
    peak = 0
    for value in samples:
        magnitude = -value if value < 0 else value
        if magnitude > peak:
            peak = magnitude
    return peak


def dbfs_of(peak: int) -> float | None:
    """Peak magnitude as dBFS, or ``None`` when the stream is silent."""
    return round(20 * math.log10(peak / 32768.0), 1) if peak > 0 else None
