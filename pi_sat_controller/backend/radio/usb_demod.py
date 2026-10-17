"""Streaming upper-sideband recovery from normalized real IC-9700 IF PCM."""

import math

import numpy as np
from scipy import signal


class UsbDemodulator:
    """Select frequencies above the IF carrier and return float32 audio.

    Input and output are mono arrays at the same sample rate. The positive
    baseband sideband is selected after translation by ``if_center_hz``.
    Output remains proportional to IF amplitude, without AGC or clipping.
    The linear-phase filter introduces a delay of ``(filter_taps - 1) / 2``
    samples. Its history and the carrier oscillator continue across blocks.
    """

    def __init__(
        self,
        sample_rate: int = 48_000,
        if_center_hz: float = 12_000.0,
        audio_low_cutoff_hz: float = 100.0,
        audio_high_cutoff_hz: float = 10_000.0,
        output_gain: float = 1.0,
        filter_taps: int = 1025,
    ) -> None:
        if isinstance(sample_rate, bool) or not isinstance(sample_rate, int) or sample_rate <= 0:
            raise ValueError("sample_rate must be a positive integer")
        if (
            isinstance(filter_taps, bool)
            or not isinstance(filter_taps, int)
            or filter_taps < 31
            or filter_taps % 2 != 1
        ):
            raise ValueError("filter_taps must be an odd integer of at least 31")
        values = (if_center_hz, audio_low_cutoff_hz, audio_high_cutoff_hz, output_gain)
        if not all(math.isfinite(value) and value > 0 for value in values):
            raise ValueError("frequencies and output_gain must be positive and finite")
        if not audio_low_cutoff_hz < audio_high_cutoff_hz:
            raise ValueError("require audio_low_cutoff_hz < audio_high_cutoff_hz")
        if if_center_hz + audio_high_cutoff_hz >= sample_rate / 2.0:
            raise ValueError("the upper IF sideband must fit below Nyquist")

        self.sample_rate = sample_rate
        self.if_center_hz = float(if_center_hz)
        self.audio_low_cutoff_hz = float(audio_low_cutoff_hz)
        self.audio_high_cutoff_hz = float(audio_high_cutoff_hz)
        self.output_gain = float(output_gain)
        self.filter_taps = filter_taps
        self._mixer_step = -math.tau * self.if_center_hz / sample_rate

        # Modulating a lowpass impulse response yields a complex bandpass
        # that passes only positive baseband frequencies. A real bandpass
        # would also pass the lower sideband and fold it into the audio.
        width = (audio_high_cutoff_hz - audio_low_cutoff_hz) / 2.0
        midpoint = (audio_high_cutoff_hz + audio_low_cutoff_hz) / 2.0
        prototype = signal.firwin(
            filter_taps, width, fs=sample_rate, window=("kaiser", 8.0)
        )
        positions = np.arange(filter_taps) - (filter_taps - 1) / 2.0
        self._sideband_fir = (
            prototype * np.exp(1j * math.tau * midpoint * positions / sample_rate)
        ).astype(np.complex64)
        self._denominator = np.array([1.0], dtype=np.float32)
        self.reset()

    def reset(self) -> None:
        """Clear carrier phase, filter history, and cumulative diagnostics."""
        self._mixer_phase = 0.0
        self._sideband_state = np.zeros(self.filter_taps - 1, dtype=np.complex64)
        self.samples_processed = 0
        self.input_clipping_count = 0
        self.output_clipping_count = 0
        self.input_peak = 0.0
        self.output_peak = 0.0
        self._input_square_sum = 0.0
        self._output_square_sum = 0.0

    @property
    def metrics(self) -> dict[str, int | float]:
        """Cumulative input and output levels for stream diagnostics."""
        count = self.samples_processed
        return {
            "samples_processed": count,
            "input_rms": math.sqrt(self._input_square_sum / count) if count else 0.0,
            "input_peak": self.input_peak,
            "input_clipping_count": self.input_clipping_count,
            "output_rms": math.sqrt(self._output_square_sum / count) if count else 0.0,
            "output_peak": self.output_peak,
            "output_clipping_count": self.output_clipping_count,
        }

    def process(self, samples: np.ndarray) -> np.ndarray:
        """Recover USB audio from one consecutive normalized IF block."""
        source = np.asarray(samples)
        if source.ndim != 1 or source.dtype.kind != "f":
            raise TypeError("samples must be a one-dimensional array of normalized floats")
        if source.size == 0:
            return np.empty(0, dtype=np.float32)
        if not np.isfinite(source).all():
            raise ValueError("samples contain NaN or infinity")
        with np.errstate(over="ignore"):
            x = np.ascontiguousarray(source, dtype=np.float32)
        if not np.isfinite(x).all():
            raise ValueError("samples cannot be represented as finite float32 values")

        count = x.size
        phases = self._mixer_phase + self._mixer_step * np.arange(count, dtype=np.float64)
        mixed = x * np.exp(1j * phases).astype(np.complex64)
        self._mixer_phase = (self._mixer_phase + self._mixer_step * count) % math.tau
        sideband, self._sideband_state = signal.lfilter(
            self._sideband_fir, self._denominator, mixed, zi=self._sideband_state
        )
        # Mixing a real IF halves each spectral component's amplitude.
        output = (sideband.real * np.float32(2.0 * self.output_gain)).astype(np.float32)

        self.samples_processed += count
        self._input_square_sum += float(np.sum(x.astype(np.float64) ** 2))
        self._output_square_sum += float(np.sum(output.astype(np.float64) ** 2))
        self.input_peak = max(self.input_peak, float(np.max(np.abs(x))))
        self.output_peak = max(self.output_peak, float(np.max(np.abs(output))))
        self.input_clipping_count += int(np.count_nonzero(np.abs(x) >= 1.0))
        self.output_clipping_count += int(np.count_nonzero(np.abs(output) > 1.0))
        return output
