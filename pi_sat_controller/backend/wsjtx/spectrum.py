"""A bounded audio-frequency spectrum from the existing native SUB PCM feed."""

from __future__ import annotations

import math
import threading
import time
from collections.abc import Callable
from typing import Any

import numpy as np


_TARGET_BIN_HZ = 3.0
_MIN_FFT_SIZE = 1024
_MAX_FFT_SIZE = 65536
_MAX_BINS = 1024
_UPDATE_SECONDS = 0.5
_STALE_SECONDS = 2.0
_FLOOR_DBFS = -100.0
_FALLBACK_SPAN_HZ = 3000.0
_MAX_SSB_BANDWIDTH_HZ = 3600.0
_MAX_AVERAGING_GAP_SECONDS = 1.5


class WsjtxAudioSpectrum:
    """Measure SUB AF without owning audio subscriptions or radio controls.

    ``feed`` accepts complete signed 16-bit little-endian mono samples. Odd
    byte counts and invalid sample rates raise ValueError without changing the
    previous spectrum. The display span uses valid cached SSB bandwidth readback;
    other modes or unavailable widths use an explicitly estimated 3000 Hz span.
    """

    def __init__(self, *, get_controller: Callable[[], Any]) -> None:
        self._get_controller = get_controller
        self._lock = threading.Lock()
        self._compute_lock = threading.Lock()
        self._buffer: np.ndarray | None = None
        self._capacity = 0
        self._write_index = 0
        self._sample_count = 0
        self._sample_rate = 0
        self._fft_size = 0
        self._window: np.ndarray | None = None
        self._window_sum = 0.0
        self._power: np.ndarray | None = None
        self._previous_power: np.ndarray | None = None
        self._last_frame_at: float | None = None
        self._averaging_seconds = 0.0
        self._bins_cache: list[float] | None = None
        self._bins_cache_key: tuple[int, float] | None = None
        self._last_compute_attempt_at: float | None = None
        self._computed_data_generation: int | None = None
        self._data_generation = 0
        self._buffer_epoch = 0
        self._last_input_at: float | None = None
        self._generation = 0
        self._timestamp_ms: int | None = None
        self._fft_compute_ms: float | None = None
        self._averaging_frames = 0
        self._display_error = ""

    def feed(self, pcm: bytes, sample_rate: int) -> None:
        if type(sample_rate) is not int or not 1000 <= sample_rate <= 192000:
            raise ValueError("Spectrum sample rate must be an integer from 1000 to 192000 Hz")
        if not isinstance(pcm, (bytes, bytearray)) or len(pcm) % 2:
            raise ValueError("Spectrum audio must contain complete S16LE mono samples")
        if not pcm:
            return
        samples = np.frombuffer(pcm, dtype="<i2")
        now = time.monotonic()
        with self._lock:
            if sample_rate != self._sample_rate:
                self._clear_locked()
                self._sample_rate = sample_rate
                self._fft_size = self._size_for_rate(sample_rate)
                self._capacity = self._fft_size
                self._buffer = np.empty(self._capacity, dtype=np.int16)
            elif (self._last_input_at is not None
                  and now - self._last_input_at > _STALE_SECONDS):
                self._clear_locked(preserve_buffer=True)
            if self._buffer is None:
                return
            self._last_input_at = now
            count = min(len(samples), self._capacity)
            if not count:
                return
            newest = samples[-count:]
            if count == self._capacity:
                self._buffer[:] = newest
                self._write_index = 0
                self._sample_count = self._capacity
            else:
                first = min(count, self._capacity - self._write_index)
                self._buffer[self._write_index:self._write_index + first] = newest[:first]
                remaining = count - first
                if remaining:
                    self._buffer[:remaining] = newest[first:]
                self._write_index = (self._write_index + count) % self._capacity
                self._sample_count = min(self._capacity, self._sample_count + count)
            self._data_generation += 1

    def snapshot(self) -> dict[str, Any]:
        bandwidth, filter_number, connected, mode, reported_width = self._radio_metadata()
        now = time.monotonic()
        if self._compute_lock.acquire(blocking=False):
            try:
                try:
                    self._compute_if_due(now)
                except Exception:
                    with self._lock:
                        self._average_failure_locked("Spectrum computation unavailable")
            finally:
                self._compute_lock.release()
        try:
            with self._lock:
                return self._snapshot_locked(
                    bandwidth, filter_number, connected, mode, reported_width, now,
                )
        except Exception:
            with self._lock:
                self._average_failure_locked("Spectrum display unavailable")
                return self._snapshot_locked(
                    bandwidth, filter_number, connected, mode, reported_width, now,
                )

    def reset(self) -> None:
        with self._lock:
            self._clear_locked()
            self._sample_rate = 0
            self._fft_size = 0

    @staticmethod
    def _size_for_rate(sample_rate: int) -> int:
        needed = max(1, math.ceil(sample_rate / _TARGET_BIN_HZ))
        size = 1 << (needed - 1).bit_length()
        return max(_MIN_FFT_SIZE, min(_MAX_FFT_SIZE, size))

    def _clear_locked(self, *, preserve_buffer: bool = False) -> None:
        if not preserve_buffer:
            self._buffer = None
            self._capacity = 0
        self._write_index = 0
        self._sample_count = 0
        self._power = None
        self._previous_power = None
        self._last_frame_at = None
        self._averaging_seconds = 0.0
        self._bins_cache = None
        self._bins_cache_key = None
        self._last_compute_attempt_at = None
        self._computed_data_generation = None
        self._data_generation += 1
        self._buffer_epoch += 1
        self._last_input_at = None
        self._generation += 1
        self._timestamp_ms = None
        self._fft_compute_ms = None
        self._averaging_frames = 0
        self._display_error = ""

    def _ring_copy_locked(self) -> np.ndarray:
        assert self._buffer is not None
        if self._sample_count < self._capacity:
            return self._buffer[:self._sample_count].copy()
        output = np.empty(self._capacity, dtype=np.int16)
        tail = self._capacity - self._write_index
        output[:tail] = self._buffer[self._write_index:]
        output[tail:] = self._buffer[:self._write_index]
        return output

    def _compute_if_due(self, now: float) -> None:
        with self._lock:
            if (self._sample_count < self._capacity or self._capacity == 0 or
                    self._computed_data_generation == self._data_generation):
                return
            if (self._last_compute_attempt_at is not None
                    and now - self._last_compute_attempt_at < _UPDATE_SECONDS):
                return
            self._last_compute_attempt_at = now
            samples = self._ring_copy_locked()
            sample_rate, fft_size = self._sample_rate, self._fft_size
            data_generation, buffer_epoch = self._data_generation, self._buffer_epoch
        try:
            if self._window is None or len(self._window) != fft_size:
                self._window = np.hanning(fft_size)
                self._window_sum = float(self._window.sum())
            values = samples.astype(np.float64)
            magnitude = np.abs(np.fft.rfft(values * self._window))
            magnitude *= 2.0 / (32768.0 * self._window_sum)
            magnitude[[0, -1]] *= 0.5
            power = np.square(magnitude)
            compute_ms = max(0.0, (time.monotonic() - now) * 1000.0)
            timestamp_ms = int(time.time() * 1000)
            with self._lock:
                if (self._sample_rate != sample_rate or self._fft_size != fft_size
                        or self._buffer_epoch != buffer_epoch):
                    return
                elapsed = None if self._last_frame_at is None else now - self._last_frame_at
                self._averaging_seconds = fft_size / sample_rate
                if (self._previous_power is not None
                        and self._previous_power.shape == power.shape
                        and elapsed is not None and 0 <= elapsed <= _MAX_AVERAGING_GAP_SECONDS):
                    self._power = (self._previous_power + power) * 0.5
                    self._averaging_frames = 2
                    self._averaging_seconds += elapsed
                else:
                    self._power = power
                    self._averaging_frames = 1
                self._previous_power = power
                self._last_frame_at = now
                self._computed_data_generation = data_generation
                self._fft_compute_ms = round(compute_ms, 3)
                self._timestamp_ms = timestamp_ms
                self._generation += 1
                self._bins_cache = None
                self._bins_cache_key = None
                self._display_error = ""
        except Exception:
            with self._lock:
                if self._buffer_epoch == buffer_epoch:
                    self._average_failure_locked("Spectrum computation unavailable")

    def _average_failure_locked(self, detail: str) -> None:
        self._power = None
        self._previous_power = None
        self._last_frame_at = None
        self._averaging_seconds = 0.0
        self._bins_cache = None
        self._bins_cache_key = None
        self._computed_data_generation = self._data_generation
        self._generation += 1
        self._timestamp_ms = None
        self._fft_compute_ms = None
        self._averaging_frames = 0
        self._display_error = detail

    def _snapshot_locked(
        self, bandwidth: float | None, filter_number: int | None,
        connected: bool, mode: str | None, reported_width: float | None,
        now: float,
    ) -> dict[str, Any]:
        estimated = bandwidth is None
        span = bandwidth if bandwidth is not None else _FALLBACK_SPAN_HZ
        if self._sample_rate:
            span = min(span, self._sample_rate / 2.0)
        age = None if self._last_input_at is None else max(0.0, now - self._last_input_at)
        available = bool(
            connected and self._sample_count >= self._capacity
            and self._power is not None and self._timestamp_ms is not None
            and age is not None and age <= _STALE_SECONDS
        )
        bins = self._display_bins_locked(span) if available else []
        resolution = (
            self._sample_rate / self._fft_size if self._sample_rate and self._fft_size else 0.0
        )
        return {
            "available": available,
            "span_hz": span,
            "bandwidth_hz": bandwidth,
            "reported_bandwidth_hz": reported_width,
            "receive_mode": mode,
            "ssb_compatible": mode in ("USB", "LSB", "USB-DATA", "LSB-DATA") if mode else None,
            "filter": filter_number,
            "estimated": estimated,
            "sample_rate": self._sample_rate,
            "fft_size": self._fft_size,
            "frame_seconds": round(self._fft_size / self._sample_rate, 3) if self._sample_rate else 0.0,
            "fft_resolution_hz": round(resolution, 3),
            "bin_hz": span / (len(bins) - 1) if len(bins) > 1 else 0.0,
            "display_bin_hz": (
                round(span / (len(bins) - 1), 3) if len(bins) > 1 else 0.0
            ),
            "bins": list(bins),
            "generation": self._generation,
            "timestamp_ms": self._timestamp_ms,
            "age_seconds": round(age, 3) if age is not None else None,
            "averaging_frames": self._averaging_frames,
            "averaging_seconds": round(self._averaging_seconds, 3),
            "fft_compute_ms": self._fft_compute_ms,
            "detail": self._display_error,
            "source": "Pi-Sat SUB AF",
        }

    def _display_bins_locked(self, span: float) -> list[float]:
        if self._power is None:
            return []
        cache_key = (self._generation, span)
        if self._bins_cache is not None and self._bins_cache_key == cache_key:
            return self._bins_cache
        levels = np.clip(
            10.0 * np.log10(np.maximum(self._power, 1e-10)),
            _FLOOR_DBFS, 0.0,
        )
        count = min(
            len(levels),
            math.floor(span * self._fft_size / self._sample_rate) + 1,
        )
        displayed = min(_MAX_BINS, count)
        if displayed < 2:
            result = [round(float(levels[0]), 2)]
            self._bins_cache, self._bins_cache_key = result, cache_key
            return result
        frequencies = np.arange(count) * self._sample_rate / self._fft_size
        buckets = np.rint(frequencies * (displayed - 1) / span).astype(np.intp)
        output = np.full(displayed, _FLOOR_DBFS)
        # Preserve narrow signals when several FFT bins share a display pixel.
        np.maximum.at(output, buckets, levels[:count])
        result = np.round(output, 2).tolist()
        self._bins_cache, self._bins_cache_key = result, cache_key
        return result

    def _radio_metadata(self) -> tuple[float | None, int | None, bool, str | None, float | None]:
        try:
            controller = self._get_controller()
            if controller is None:
                return None, None, False, None, None
            # try_snapshot reads cached state and never issues a CAT command.
            state = controller.try_snapshot()
            if not isinstance(state, dict):
                return None, None, True, None, None
            sub = state.get("sub") or {}
            width = sub.get("bandwidth_hz")
            bandwidth = float(width) if isinstance(width, (int, float)) and not isinstance(width, bool) else None
            if bandwidth is not None and (not math.isfinite(bandwidth) or bandwidth <= 0):
                bandwidth = None
            reported_width = bandwidth
            mode = sub.get("mode")
            mode = mode.upper() if isinstance(mode, str) and mode else None
            if mode in ("USB", "LSB") and sub.get("data_mode") is True:
                mode += "-DATA"
            # FM/DV IF filter widths do not describe the demodulated AF span.
            # SSB widths beyond the IC-9700's range are stale or invalid metadata.
            if ((mode and mode not in ("USB", "LSB", "USB-DATA", "LSB-DATA"))
                    or (bandwidth is not None and bandwidth > _MAX_SSB_BANDWIDTH_HZ)):
                bandwidth = None
            filter_number = sub.get("filter")
            if type(filter_number) is not int or filter_number not in (1, 2, 3):
                filter_number = None
            return bandwidth, filter_number, state.get("connected") is not False, mode, reported_width
        except Exception:
            return None, None, True, None, None
