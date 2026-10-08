"""PD120/PD180 two-row physical scans and fractional channel extraction.

Dayton 2000 pages 9-10 specify VIS 95, 640x496 pixels, a 20 ms sync,
2.080 ms porch, and four 121.600 ms scans: Y(first), Cr, Cb, Y(second).
Cr and Cb are shared by both rows; each channel retains 640 horizontal samples.
PD180 (VIS 96) retains that layout with 183.040 ms channel scans.
The caller supplies its pixel reader and owns timing, frequency correction,
signal acquisition, and progressive row publication.
"""
import math

WIDTH = 640
HEIGHT = 496
PAIRS = HEIGHT // 2
VIS_CODE = 95
SYNC_SECONDS = .020
PORCH_SECONDS = .002080
CHANNEL_SECONDS = .121600
PAIR_SECONDS = SYNC_SECONDS + PORCH_SECONDS + 4 * CHANNEL_SECONDS


def channel_layout(rate, start, actual_period, horizontal_ms=0, *, mode="PD120"):
    """Return ``channel: (start_sample, duration_samples)`` for a physical pair.

    ``start`` is its sync edge and ``actual_period`` is the measured pair period
    in samples. Durations and relative starts scale with that measured period;
    horizontal adjustment is an absolute time shift in milliseconds. Positions
    remain fractional, so repeated pairs do not accumulate integer rounding.
    """
    if not all(math.isfinite(v) for v in (rate, start, actual_period, horizontal_ms)):
        raise ValueError("PD120 timing values must be finite")
    if rate <= 0 or actual_period <= 0:
        raise ValueError("PD120 rate and pair period must be positive")
    if mode not in ("PD120", "PD180"):
        raise ValueError("PD extraction mode must be PD120 or PD180")
    channel_seconds = .183040 if mode == "PD180" else CHANNEL_SECONDS
    pair_seconds = SYNC_SECONDS + PORCH_SECONDS + 4 * channel_seconds
    samples_per_protocol_second = actual_period / pair_seconds
    first = start + (SYNC_SECONDS + PORCH_SECONDS) * samples_per_protocol_second
    first += horizontal_ms * .001 * rate
    duration = channel_seconds * samples_per_protocol_second
    return {name: (first + index * duration, duration)
            for index, name in enumerate(("y_first", "cr", "cb", "y_second"))}


def extract_first(pixel_reader, rate, start, period, horizontal_ms=0, chroma_reader=None,
                  *, separate_quality=False, mode="PD120"):
    """Read the first Y and both shared chroma scans, returning Y/Cr/Cb/quality.

    ``pixel_reader(start, duration, count)`` returns intensity values and scalar
    quality after the caller's frequency correction. With ``separate_quality``,
    the last result is (Y quality, shared chroma quality), so a damaged first Y
    does not invalidate the second Y. Otherwise it is their minimum for this row.
    This row is complete once the Cb scan arrives, before the second Y finishes.
    """
    layout = channel_layout(rate, start, period, horizontal_ms, mode=mode)
    y, yq = pixel_reader(*layout["y_first"], WIDTH)
    chroma_reader=chroma_reader or pixel_reader
    cr, crq = chroma_reader(*layout["cr"], WIDTH)
    cb, cbq = chroma_reader(*layout["cb"], WIDTH)
    chroma_quality = min(crq, cbq)
    return y, cr, cb, (yq, chroma_quality) if separate_quality else min(yq, chroma_quality)


def extract_second(pixel_reader, rate, start, period, cr, cb, horizontal_ms=0, *, mode="PD120"):
    """Read second Y and reuse first-row chroma without resampling or mutation.

    Returned quality covers the new Y scan. The caller combines it with the
    stored shared-chroma quality from :func:`extract_first` when publishing.
    """
    layout = channel_layout(rate, start, period, horizontal_ms, mode=mode)
    y, quality = pixel_reader(*layout["y_second"], WIDTH)
    return y, cr, cb, quality
