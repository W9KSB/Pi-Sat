"""Causal receive-audio Doppler correction for the managed WSJT-X chain.

The radio removes the receiver tuning frequency from the demodulated audio.
When tracking retunes the radio, that subtraction changes in small steps.  This
module keeps a phase-continuous audio view of the same signal by shifting the
AF stream by the difference between the confirmed radio tuning and the smooth
tracking reference.

The processor deliberately uses only causal state: packet arrival timestamps,
confirmed tuning readbacks, and Doppler models that have already arrived.  It
does not ask the radio for a new frequency.
"""

from __future__ import annotations

import math
import threading
import time
from collections import deque
from collections.abc import Callable, Iterable
from typing import Any

import numpy as np


_DEFAULT_TAPS = 513
_MIN_TAPS = 65
_MAX_TAPS = 2049
_MAX_SHIFT_HZ = 8000.0
_MAX_RATE_HZ_S = 500.0
_MAX_MODEL_HORIZON_S = 3.0
_MAX_MODEL_AGE_S = 3.0
_MAX_CLOCK_DRIFT_S = 0.25
_CONTINUITY_COUNTERS = (
    "rx_audio_sequence_gaps", "rx_audio_sequence_skipped",
    "rx_audio_duplicates", "rx_audio_out_of_order",
)


def _finite(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _ssb_mode(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    mode = value.strip().upper().replace("_", "-")
    if mode in ("USB", "USB-DATA"):
        return "USB"
    if mode in ("LSB", "LSB-DATA"):
        return "LSB"
    return None


class DopplerAudioProcessor:
    """Streaming Hilbert FIR and NCO with exact input/output sample counts."""

    def __init__(self, sample_rate: int, taps: int = _DEFAULT_TAPS) -> None:
        if type(sample_rate) is not int or not 8000 <= sample_rate <= 192000:
            raise ValueError("Doppler sample rate must be an integer from 8000 to 192000 Hz")
        if type(taps) is not int or taps < _MIN_TAPS or taps > _MAX_TAPS or taps % 2 == 0:
            raise ValueError(f"Doppler FIR taps must be an odd integer from {_MIN_TAPS} to {_MAX_TAPS}")
        self.sample_rate = sample_rate
        self.taps = taps
        self.delay_samples = taps // 2
        indices = np.arange(-self.delay_samples, self.delay_samples + 1, dtype=np.float64)
        coefficients = np.zeros(taps, dtype=np.float64)
        odd = np.abs(indices.astype(np.int64)) % 2 == 1
        coefficients[odd] = 2.0 / (math.pi * indices[odd])
        # A tapered odd-length Hilbert transformer gives useful image
        # suppression at low AF offsets without an external filter design
        # dependency.  The zero center tap keeps the real path aligned.
        coefficients *= np.hanning(taps)
        coefficients[self.delay_samples] = 0.0
        self._coefficients = coefficients
        self._history = np.zeros(taps - 1, dtype=np.float64)
        self._phase = 0.0

    @property
    def phase(self) -> float:
        return self._phase

    def reset(self) -> None:
        self._history.fill(0.0)
        self._phase = 0.0

    def process(
        self,
        samples: np.ndarray | Iterable[float],
        *,
        sample_times: np.ndarray | Iterable[float] | None = None,
        actual_rf_hz: np.ndarray | Iterable[float] | float,
        target_rf_hz: np.ndarray | Iterable[float] | float,
        mode: str = "USB",
    ) -> np.ndarray:
        """Return a same-length corrected S16 sample array.

        ``sample_times`` are the times represented by the *output* samples,
        after the FIR's group delay.  Supplying them explicitly lets the
        runtime align tuning readbacks with the delayed signal rather than with
        the packet that happens to be arriving now.
        """

        values = np.asarray(samples, dtype=np.float64)
        if values.ndim != 1:
            raise ValueError("Doppler audio samples must be one-dimensional")
        count = values.size
        if not count:
            return np.empty(0, dtype=np.int16)

        normalized_mode = _ssb_mode(mode)
        if normalized_mode is None:
            raise ValueError("Doppler correction supports USB or LSB only")
        if sample_times is None:
            times = np.arange(count, dtype=np.float64) / self.sample_rate
        else:
            times = np.asarray(sample_times, dtype=np.float64)
            if times.ndim != 1 or times.size != count:
                raise ValueError("Doppler sample times must match the sample count")
        actual = self._broadcast(actual_rf_hz, count, "actual RF")
        target = self._broadcast(target_rf_hz, count, "target RF")
        if not (np.isfinite(times).all() and np.isfinite(actual).all() and np.isfinite(target).all()):
            raise ValueError("Doppler inputs must be finite")

        shift = actual - target if normalized_mode == "USB" else target - actual
        context = np.concatenate((self._history, values))
        indices = self._history.size + np.arange(count, dtype=np.intp)
        real = context[indices - self.delay_samples]
        imaginary = np.convolve(context, self._coefficients, mode="valid")
        phase = self._phase + np.cumsum(2.0 * math.pi * shift / self.sample_rate)
        self._phase = float(phase[-1] % (2.0 * math.pi))
        analytic = real + 1j * imaginary
        output = np.real(analytic * np.exp(1j * phase))
        self._history = context[-(self.taps - 1):].copy()
        return np.rint(np.clip(output, -32768.0, 32767.0)).astype(np.int16)

    @staticmethod
    def _broadcast(value: np.ndarray | Iterable[float] | float, count: int, label: str) -> np.ndarray:
        array = np.asarray(value, dtype=np.float64)
        if array.ndim == 0:
            return np.full(count, float(array), dtype=np.float64)
        array = np.ravel(array)
        if array.size != count:
            raise ValueError(f"Doppler {label} values must be scalar or match the sample count")
        return array


class WsjtxRxDoppler:
    """Runtime adapter for the managed WSJT-X receive-audio bridge."""

    def __init__(
        self,
        get_controller: Callable[[], Any],
        get_tracking_manager: Callable[[], Any] | None = None,
        *,
        taps: int = _DEFAULT_TAPS,
    ) -> None:
        self._get_controller = get_controller
        self._get_tracking_manager = get_tracking_manager
        self._taps = taps
        self._lock = threading.RLock()
        self._processor: DopplerAudioProcessor | None = None
        self._sample_rate = 0
        self._next_input_time: float | None = None
        self._last_identity: Any = None
        self._models: deque[dict[str, Any]] = deque(maxlen=96)
        self._last_reference_rf_hz: float | None = None
        self._last_mode: str | None = None
        self._last_actual_rf_hz: int | None = None
        self._connection_generation: Any = None
        self._source_controller: Any = None
        self._continuity: tuple[int | None, ...] | None = None
        self._discontinuity_pending = False
        self._state = "bypass"
        self._reason = "waiting for causal Doppler telemetry"
        self._packets = 0
        self._corrected_samples = 0
        self._bypassed_samples = 0
        self._clock_resets = 0
        self._context_resets = 0

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            processor = self._processor
            return {
                "state": self._state,
                "active": self._state == "active",
                "reason": self._reason,
                "sample_rate": self._sample_rate,
                "mode": self._last_mode,
                "reference_rf_hz": self._last_reference_rf_hz,
                "actual_rf_hz": self._last_actual_rf_hz,
                "shift_hz": self._applied_shift_hz(),
                "filter_delay_samples": processor.delay_samples if processor is not None else 0,
                "filter_taps": processor.taps if processor is not None else self._taps,
                "packets": self._packets,
                "corrected_samples": self._corrected_samples,
                "bypassed_samples": self._bypassed_samples,
                "clock_resets": self._clock_resets,
                "context_resets": self._context_resets,
            }

    def reset(self, reason: str = "reset") -> None:
        with self._lock:
            if self._processor is not None:
                self._processor.reset()
            self._next_input_time = None
            self._models.clear()
            self._last_identity = None
            self._last_reference_rf_hz = None
            self._last_actual_rf_hz = None
            self._last_mode = None
            self._connection_generation = None
            self._source_controller = None
            self._continuity = None
            self._discontinuity_pending = False
            self._state = "bypass"
            self._reason = reason
            self._context_resets += 1

    def process_pcm(self, pcm: bytes, sample_rate: int, arrival_monotonic: float) -> bytes:
        """Correct one mono S16LE packet in place of the decoder playback path."""

        if not isinstance(pcm, (bytes, bytearray)) or len(pcm) % 2:
            raise ValueError("Doppler RX audio must contain complete S16LE mono samples")
        if not pcm:
            return b""
        if type(sample_rate) is not int or not 8000 <= sample_rate <= 192000:
            raise ValueError("Doppler RX audio sample rate is invalid")
        arrival = _finite(arrival_monotonic)
        if arrival is None:
            arrival = time.monotonic()

        with self._lock:
            self._ensure_processor(sample_rate)
            controller = self._safe_controller()
            if self._source_controller is not controller:
                if self._source_controller is not None:
                    self.reset("Native receive source changed")
                self._source_controller = controller
            state = self._safe_snapshot(controller)
            model = self._read_model()
            history = self._read_history(controller)
            generation = history[-1][3] if history else None
            history_mode, has_history_mode = self._latest_history_mode(history, arrival)
            mode = history_mode if has_history_mode else self._mode_from_state(state)
            self._remember_model(model)
            self._packets += 1
            model_usable = model is not None and arrival - float(model["sample_time"]) <= _MAX_MODEL_AGE_S
            connected = (
                state.get("connected") is not False
                if isinstance(state, dict)
                else bool(history)
            )

            if generation != self._connection_generation and self._connection_generation is not None:
                self._context_resets += 1
                self._processor.reset()
                self._next_input_time = None
                self._continuity = None
                self._models.clear()
                self._remember_model(model)
                self._last_identity = None
                self._reason = "radio connection changed"
            self._connection_generation = generation
            if isinstance(state, dict):
                continuity = tuple(
                    state[key] if type(state.get(key)) is int else None
                    for key in _CONTINUITY_COUNTERS
                )
                if self._continuity is not None and any(
                    before is not None and after is not None and before != after
                    for before, after in zip(self._continuity, continuity)
                ):
                    self._discontinuity_pending = True
                self._continuity = continuity

            samples = np.frombuffer(pcm, dtype="<i2").astype(np.float64)
            count = samples.size
            input_times, clock_reset = self._input_times(count, arrival, sample_rate)
            if clock_reset and not self._discontinuity_pending:
                self._clock_resets += 1
                self._processor.reset()
                self._models.clear()
                self._remember_model(model)
                self._reason = "audio clock discontinuity"
            output_times = input_times - self._processor.delay_samples / sample_rate

            identity = model.get("identity") if isinstance(model, dict) else None
            if identity != self._last_identity and self._last_identity is not None:
                self._context_resets += 1
                self._processor.reset()
                self._models.clear()
                self._remember_model(model)
                self._reason = "tracking context changed"
            self._last_identity = identity
            self._last_mode = mode

            actual, actual_valid = self._actual_rf_for_times(
                output_times, history, state, mode, generation
            )
            target, target_valid = self._target_rf_for_times(output_times, model)
            valid = actual_valid & target_valid
            if not model_usable or not connected or self._discontinuity_pending:
                valid[:] = False
            if mode is None:
                valid[:] = False
            shift = np.zeros(count, dtype=np.float64)
            if valid.any():
                if mode == "USB":
                    shift[valid] = actual[valid] - target[valid]
                else:
                    shift[valid] = target[valid] - actual[valid]
            if model is None:
                self._state, self._reason = "bypass", "no active causal Doppler model"
            elif not model_usable:
                self._state, self._reason = "bypass", "causal Doppler model is stale"
            elif not connected:
                self._state, self._reason = "bypass", "radio is not connected"
            elif mode is None:
                self._state, self._reason = "bypass", "receive mode is not USB/LSB"
            elif self._discontinuity_pending:
                self._state, self._reason = "bypass", "native audio continuity changed; draining queued PCM"
            elif valid.any() and np.max(np.abs(shift[valid])) > _MAX_SHIFT_HZ:
                valid[:] = False
                self._state, self._reason = "bypass", "Doppler shift exceeds safe range"
            elif not valid.all():
                self._state, self._reason = "bypass", "waiting for causal RF/model samples"
            else:
                self._state, self._reason = "active", "causal continuous Doppler correction"

            if valid.any():
                last = np.flatnonzero(valid)[-1]
                self._last_reference_rf_hz = float(target[last])
                self._last_actual_rf_hz = int(round(float(actual[last])))
                self._corrected_samples += int(valid.sum())
            self._bypassed_samples += int((~valid).sum())
            # Invalid samples use actual == target, which is a zero-Hz shift
            # while still passing through the FIR/NCO state.  Bypass and active
            # output therefore have the same latency.
            output = self._processor.process(
                samples,
                sample_times=output_times,
                actual_rf_hz=np.where(valid, actual, target),
                target_rf_hz=target,
                mode=mode or "USB",
            )
            return output.astype("<i2", copy=False).tobytes()

    def finish_batch(self) -> None:
        """Re-anchor after draining PCM whose exact loss boundary is unknown.

        Transport counters can describe a gap later in an already queued
        batch. Bypass that batch and reset at its end, so the next packet's
        content timestamp anchors the recovered stream.
        """
        with self._lock:
            if self._discontinuity_pending:
                self._processor.reset()
                self._next_input_time = None
                self._discontinuity_pending = False
                self._clock_resets += 1
                self._last_reference_rf_hz = None
                self._last_actual_rf_hz = None
                self._state, self._reason = "bypass", "waiting for PCM after native audio discontinuity"

    def rx_audio_frequency_hz(self, offset_hz: int) -> int | None:
        """Map a corrected decoder DF to physical RX RF for contact logging."""

        if type(offset_hz) is not int or isinstance(offset_hz, bool):
            return None
        with self._lock:
            if self._state != "active" or self._last_reference_rf_hz is None:
                return None
            if self._last_mode == "USB":
                return int(round(self._last_reference_rf_hz + offset_hz))
            if self._last_mode == "LSB":
                return int(round(self._last_reference_rf_hz - offset_hz))
            return None

    def _ensure_processor(self, sample_rate: int) -> None:
        if self._processor is None or self._sample_rate != sample_rate:
            self._processor = DopplerAudioProcessor(sample_rate, self._taps)
            self._sample_rate = sample_rate
            self._next_input_time = None
            self._last_identity = None
            self._continuity = None
            self._discontinuity_pending = False
            self._models.clear()

    def _input_times(self, count: int, arrival: float, sample_rate: int) -> tuple[np.ndarray, bool]:
        if self._next_input_time is None:
            start = arrival - (count - 1) / sample_rate
            self._next_input_time = start + count / sample_rate
            return start + np.arange(count, dtype=np.float64) / sample_rate, False
        start = self._next_input_time
        drift = arrival - start
        if abs(drift) > max(_MAX_CLOCK_DRIFT_S, 2.0 * count / sample_rate):
            start = arrival - (count - 1) / sample_rate
            self._next_input_time = start + count / sample_rate
            return start + np.arange(count, dtype=np.float64) / sample_rate, True
        self._next_input_time = start + count / sample_rate
        return start + np.arange(count, dtype=np.float64) / sample_rate, False

    def _safe_controller(self) -> Any:
        try:
            return self._get_controller()
        except Exception:
            return None

    @staticmethod
    def _safe_snapshot(controller: Any) -> dict[str, Any] | None:
        if controller is None:
            return None
        try:
            reader = getattr(controller, "try_snapshot", None)
            result = reader() if callable(reader) else controller.snapshot()
            return result if isinstance(result, dict) else None
        except Exception:
            return None

    @staticmethod
    def _mode_from_state(state: dict[str, Any] | None) -> str | None:
        if not isinstance(state, dict):
            return None
        sub = state.get("sub")
        if not isinstance(sub, dict):
            return None
        return _ssb_mode(sub.get("mode"))

    @staticmethod
    def _latest_history_mode(
        history: list[tuple[float, int, str | None, Any]], when: float
    ) -> tuple[str | None, bool]:
        for event in reversed(history):
            if event[0] <= when:
                return event[2], True
        return None, False

    def _applied_shift_hz(self) -> float | None:
        if self._state != "active" or self._last_actual_rf_hz is None or self._last_reference_rf_hz is None:
            return None
        if self._last_mode == "USB":
            return float(self._last_actual_rf_hz) - self._last_reference_rf_hz
        if self._last_mode == "LSB":
            return self._last_reference_rf_hz - float(self._last_actual_rf_hz)
        return None

    def _read_model(self) -> dict[str, Any] | None:
        if self._get_tracking_manager is None:
            return None
        try:
            manager = self._get_tracking_manager()
            reader = getattr(manager, "audio_doppler_state", None)
            result = reader() if callable(reader) else None
        except Exception:
            return None
        if not isinstance(result, dict):
            return None
        sample_time = _finite(result.get("sample_time"))
        target = _finite(result.get("target_hz"))
        rate = _finite(result.get("rate_hz_s"))
        if sample_time is None or target is None or rate is None:
            return None
        if abs(rate) > _MAX_RATE_HZ_S:
            return None
        return {
            "sample_time": sample_time,
            "target_hz": target,
            "rate_hz_s": rate,
            "identity": result.get("identity"),
        }

    def _read_history(self, controller: Any) -> list[tuple[float, int, str | None, Any]]:
        if controller is None:
            return []
        reader = getattr(controller, "rx_tuning_history", None)
        if not callable(reader):
            return []
        try:
            raw = reader(side="SUB")
        except TypeError:
            try:
                raw = reader("SUB")
            except Exception:
                return []
        except Exception:
            return []
        if not isinstance(raw, (list, tuple)):
            return []
        result: list[tuple[float, int, str | None, Any]] = []
        for item in raw:
            if not isinstance(item, (list, tuple)) or len(item) < 4:
                continue
            when = _finite(item[0])
            frequency = _finite(item[1])
            if when is None or frequency is None:
                continue
            mode = item[2] if isinstance(item[2], str) or item[2] is None else None
            result.append((when, int(round(frequency)), _ssb_mode(mode), item[3]))
        result.sort(key=lambda entry: entry[0])
        return result

    def _remember_model(self, model: dict[str, Any] | None) -> None:
        if model is None:
            return
        identity = model.get("identity")
        if any(
            existing["identity"] == identity
            and existing["sample_time"] == model["sample_time"]
            for existing in self._models
        ):
            return
        self._models.append(dict(model))

    def _actual_rf_for_times(
        self,
        times: np.ndarray,
        history: list[tuple[float, int, str | None, Any]],
        state: dict[str, Any] | None,
        mode: str | None,
        generation: Any,
    ) -> tuple[np.ndarray, np.ndarray]:
        count = times.size
        actual = np.zeros(count, dtype=np.float64)
        valid = np.zeros(count, dtype=bool)
        if history:
            event_times = np.asarray([entry[0] for entry in history], dtype=np.float64)
            event_frequencies = np.asarray([entry[1] for entry in history], dtype=np.float64)
            event_modes = np.asarray([entry[2] for entry in history], dtype=object)
            event_generations = np.asarray([entry[3] for entry in history], dtype=object)
            positions = np.searchsorted(event_times, times, side="right") - 1
            valid = positions >= 0
            selected = positions[valid]
            valid[valid] = (
                (event_modes[selected] == mode)
                & (event_generations[selected] == generation)
            )
            actual[valid] = event_frequencies[positions[valid]]
        return actual, valid

    def _target_rf_for_times(
        self, times: np.ndarray, current: dict[str, Any] | None
    ) -> tuple[np.ndarray, np.ndarray]:
        count = times.size
        target = np.zeros(count, dtype=np.float64)
        valid = np.zeros(count, dtype=bool)
        if current is None or current.get("identity") is None:
            return target, valid
        identity = current["identity"]
        candidates = [model for model in self._models if model.get("identity") == identity]
        if not candidates:
            return target, valid
        model_times = np.asarray([model["sample_time"] for model in candidates], dtype=np.float64)
        model_targets = np.asarray([model["target_hz"] for model in candidates], dtype=np.float64)
        model_rates = np.asarray([model["rate_hz_s"] for model in candidates], dtype=np.float64)
        order = np.argsort(model_times)
        model_times = model_times[order]
        model_targets = model_targets[order]
        model_rates = model_rates[order]
        positions = np.searchsorted(model_times, times, side="right") - 1
        positioned = positions >= 0
        output_indices = np.flatnonzero(positioned)
        selected = positions[output_indices]
        ages = times[output_indices] - model_times[selected]
        usable = ages <= _MAX_MODEL_HORIZON_S
        output_indices = output_indices[usable]
        selected = selected[usable]
        ages = ages[usable]
        valid[output_indices] = True
        target[output_indices] = model_targets[selected] + model_rates[selected] * ages
        return target, valid
