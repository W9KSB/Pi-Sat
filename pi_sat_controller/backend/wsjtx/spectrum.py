"""A bounded audio-frequency spectrum from the existing native SUB PCM feed."""

from __future__ import annotations

import math
import threading
import time
from collections.abc import Callable
from typing import Any

import numpy as np


_WINDOW_SAMPLES = 4096
_MAX_BINS = 512
_UPDATE_SECONDS = 0.2
_STALE_SECONDS = 2.0
_FLOOR_DBFS = -100.0
_FALLBACK_SPAN_HZ = 3000.0


class WsjtxAudioSpectrum:
    """Measure SUB AF without owning audio subscriptions or radio controls.

    ``feed`` accepts complete signed 16-bit little-endian mono samples. Odd
    byte counts and invalid sample rates raise ValueError without changing the
    previous spectrum. The display span uses cached bandwidth readback; an
    unknown bandwidth uses an explicitly estimated 3000 Hz span.
    """

    def __init__(self, *, get_controller: Callable[[], Any]) -> None:
        self._get_controller = get_controller
        self._lock = threading.Lock()
        self._window = np.hanning(_WINDOW_SAMPLES)
        self._window_sum = float(self._window.sum())
        self._samples = np.empty(0, dtype=np.float64)
        self._sample_rate = 0
        self._levels: np.ndarray | None = None
        self._last_fft_at: float | None = None
        self._last_input_at: float | None = None
        self._generation = 0

    def feed(self, pcm: bytes, sample_rate: int) -> None:
        if type(sample_rate) is not int or not 1000 <= sample_rate <= 192000:
            raise ValueError("Spectrum sample rate must be an integer from 1000 to 192000 Hz")
        if not isinstance(pcm, (bytes, bytearray)) or len(pcm) % 2:
            raise ValueError("Spectrum audio must contain complete S16LE mono samples")
        if not pcm:
            return
        # Take only the newest window before allocating the floating-point copy.
        samples = np.frombuffer(pcm, dtype="<i2")[-_WINDOW_SAMPLES:].astype(np.float64)
        now = time.monotonic()
        with self._lock:
            if (sample_rate != self._sample_rate or
                    (self._last_input_at is not None and now - self._last_input_at > _STALE_SECONDS)):
                self._clear_locked()
                self._sample_rate = sample_rate
            self._last_input_at = now
            self._samples = np.concatenate((self._samples, samples))[-_WINDOW_SAMPLES:]
            if len(self._samples) < _WINDOW_SAMPLES:
                return
            if self._last_fft_at is not None and now - self._last_fft_at < _UPDATE_SECONDS:
                return
            magnitude = np.abs(np.fft.rfft(self._samples * self._window))
            magnitude *= 2.0 / (32768.0 * self._window_sum)
            # DC and Nyquist have no negative-frequency counterpart.
            magnitude[[0, -1]] *= 0.5
            self._levels = np.clip(20.0 * np.log10(np.maximum(magnitude, 1e-5)), _FLOOR_DBFS, 0.0)
            self._last_fft_at = now
            self._generation += 1

    def snapshot(self) -> dict[str, Any]:
        bandwidth, filter_number, connected = self._radio_metadata()
        now = time.monotonic()
        with self._lock:
            estimated = bandwidth is None
            span = bandwidth if bandwidth is not None else _FALLBACK_SPAN_HZ
            if self._sample_rate:
                span = min(span, self._sample_rate / 2.0)
            age = None if self._last_fft_at is None else max(0.0, now - self._last_fft_at)
            available = connected and self._levels is not None and age is not None and age <= _STALE_SECONDS
            bins = self._display_bins_locked(span) if available else []
            return {
                "available": available,
                "span_hz": span,
                "bandwidth_hz": bandwidth,
                "filter": filter_number,
                "estimated": estimated,
                "sample_rate": self._sample_rate,
                "fft_size": _WINDOW_SAMPLES,
                "bin_hz": span / (len(bins) - 1) if len(bins) > 1 else 0.0,
                "bins": bins,
                "generation": self._generation,
                "age_seconds": round(age, 3) if age is not None else None,
                "source": "Pi-Sat SUB AF",
            }

    def reset(self) -> None:
        with self._lock:
            self._clear_locked()
            self._sample_rate = 0

    def _clear_locked(self) -> None:
        self._samples = np.empty(0, dtype=np.float64)
        self._levels = None
        self._last_fft_at = None
        self._last_input_at = None
        self._generation += 1

    def _display_bins_locked(self, span: float) -> list[float]:
        count = min(len(self._levels), math.floor(span * _WINDOW_SAMPLES / self._sample_rate) + 1)
        displayed = min(_MAX_BINS, count)
        if displayed < 2:
            return [round(float(self._levels[0]), 2)]
        frequencies = np.arange(count) * self._sample_rate / _WINDOW_SAMPLES
        buckets = np.rint(frequencies * (displayed - 1) / span).astype(np.intp)
        output = np.full(displayed, _FLOOR_DBFS)
        # Preserve narrow signals when several FFT bins share a display pixel.
        np.maximum.at(output, buckets, self._levels[:count])
        return np.round(output, 2).tolist()

    def _radio_metadata(self) -> tuple[float | None, int | None, bool]:
        try:
            controller = self._get_controller()
            if controller is None:
                return None, None, False
            # try_snapshot reads cached state and never issues a CAT command.
            state = controller.try_snapshot()
            if not isinstance(state, dict):
                return None, None, True
            sub = state.get("sub") or {}
            width = sub.get("bandwidth_hz")
            bandwidth = float(width) if isinstance(width, (int, float)) and not isinstance(width, bool) else None
            if bandwidth is not None and (not math.isfinite(bandwidth) or bandwidth <= 0):
                bandwidth = None
            filter_number = sub.get("filter")
            if type(filter_number) is not int or filter_number not in (1, 2, 3):
                filter_number = None
            return bandwidth, filter_number, state.get("connected") is not False
        except Exception:
            return None, None, True
