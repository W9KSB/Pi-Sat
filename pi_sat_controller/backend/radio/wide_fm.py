"""Streaming FM demodulation of normalized, real IC-9700 LAN IF samples.

The input and output are one-dimensional float32 arrays at the same sample
rate. This module has no WAV, radio, network, or audio-device dependencies.
"""

import math

import numpy as np
from scipy import signal


class WideFmDemodulator:
    """Translate real IF to complex baseband and recover wideband FM PCM.

    ``process`` accepts normalized floating-point IF samples (typically
    int16 PCM divided by 32768) and returns un-clipped float32 samples.
    The fixed ``output_gain`` is in output units per hertz of deviation.
    """

    def __init__(
        self,
        sample_rate: int = 48_000,
        if_center_hz: float = 11_867.0,
        channel_bandwidth_hz: float = 21_000.0,
        output_cutoff_hz: float = 10_000.0,
        dc_cutoff_hz: float = 3.0,
        output_gain: float = 1.0 / 24_000.0,
        filter_taps: int = 129,
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
        values = (if_center_hz, channel_bandwidth_hz, output_cutoff_hz, dc_cutoff_hz, output_gain)
        if not all(math.isfinite(v) and v > 0 for v in values):
            raise ValueError("frequencies and output_gain must be positive and finite")
        half_channel = channel_bandwidth_hz / 2.0
        if half_channel >= min(if_center_hz, sample_rate / 2.0 - if_center_hz):
            raise ValueError("the selected real IF channel must fit between DC and Nyquist")
        if not dc_cutoff_hz < output_cutoff_hz < sample_rate / 2.0:
            raise ValueError("require dc_cutoff_hz < output_cutoff_hz < Nyquist")

        self.sample_rate = sample_rate
        self.if_center_hz = float(if_center_hz)
        self.channel_bandwidth_hz = float(channel_bandwidth_hz)
        self.output_cutoff_hz = float(output_cutoff_hz)
        self.dc_cutoff_hz = float(dc_cutoff_hz)
        self.output_gain = float(output_gain)
        self.filter_taps = filter_taps

        self._mixer_step = -math.tau * self.if_center_hz / sample_rate
        self._fir_denominator = np.array([1.0], dtype=np.float32)
        # Linear-phase FIRs preserve the modulation waveform. Their delay is
        # carried across calls rather than trimming or padding each block.
        self._channel_fir = signal.firwin(
            filter_taps, half_channel, fs=sample_rate, window=("kaiser", 8.0)
        ).astype(np.float32)
        self._output_fir = signal.firwin(
            filter_taps, output_cutoff_hz, fs=sample_rate, window=("kaiser", 8.0)
        ).astype(np.float32)
        self._dc_sos = signal.butter(
            1, dc_cutoff_hz, btype="highpass", fs=sample_rate, output="sos"
        ).astype(np.float32)
        self.reset()

    def reset(self) -> None:
        """Clear oscillator, filters, discriminator history, and diagnostics."""
        self._mixer_phase = 0.0
        self._channel_state = np.zeros(self.filter_taps - 1, dtype=np.complex64)
        self._previous_complex = np.complex64(0.0)
        self._have_previous_complex = False
        self._dc_state = np.zeros((len(self._dc_sos), 2), dtype=np.float32)
        self._output_state = np.zeros(self.filter_taps - 1, dtype=np.float32)

        self.samples_processed = 0
        self.input_clipping_count = 0
        self.output_clipping_count = 0
        self._input_square_sum = 0.0
        self._output_square_sum = 0.0
        self._discriminator_sum_hz = 0.0
        self.input_peak = 0.0
        self.output_peak = 0.0

    @property
    def metrics(self) -> dict[str, int | float]:
        """Cumulative, inexpensive stream diagnostics."""
        count = self.samples_processed
        return {
            "samples_processed": count,
            "input_rms": math.sqrt(self._input_square_sum / count) if count else 0.0,
            "input_peak": self.input_peak,
            "input_clipping_count": self.input_clipping_count,
            "output_rms": math.sqrt(self._output_square_sum / count) if count else 0.0,
            "output_peak": self.output_peak,
            "output_clipping_count": self.output_clipping_count,
            "mean_discriminator_hz": self._discriminator_sum_hz / count if count else 0.0,
        }

    def process(self, samples: np.ndarray) -> np.ndarray:
        """Demodulate one consecutive mono IF block without resetting state."""
        source = np.asarray(samples)
        if source.ndim != 1 or source.dtype.kind != "f":
            raise TypeError("samples must be a one-dimensional array of normalized floats")
        if source.size == 0:
            return np.empty(0, dtype=np.float32)
        if not np.isfinite(source).all():
            raise ValueError("samples contain NaN or infinity")

        x = np.ascontiguousarray(source, dtype=np.float32)
        count = x.size
        phases = self._mixer_phase + self._mixer_step * np.arange(count, dtype=np.float64)
        oscillator = np.exp(1j * phases).astype(np.complex64)
        self._mixer_phase = (self._mixer_phase + self._mixer_step * count) % math.tau
        mixed = x * oscillator

        channel, self._channel_state = signal.lfilter(
            self._channel_fir,
            self._fir_denominator,
            mixed,
            zi=self._channel_state,
        )
        previous = np.empty_like(channel)
        previous[0] = self._previous_complex if self._have_previous_complex else channel[0]
        previous[1:] = channel[:-1]
        self._previous_complex = np.complex64(channel[-1])
        self._have_previous_complex = True
        discriminator_hz = np.angle(channel * np.conj(previous)).astype(np.float32)
        discriminator_hz *= np.float32(self.sample_rate / math.tau)
        # An empty FIR has no meaningful phase until its first tap history is
        # filled. Suppress only that one-time startup interval.
        startup = max(0, self.filter_taps - self.samples_processed)
        if startup:
            discriminator_hz[:startup] = 0.0

        without_dc, self._dc_state = signal.sosfilt(
            self._dc_sos, discriminator_hz, zi=self._dc_state
        )
        filtered, self._output_state = signal.lfilter(
            self._output_fir,
            self._fir_denominator,
            without_dc,
            zi=self._output_state,
        )
        output = np.asarray(filtered * np.float32(self.output_gain), dtype=np.float32)

        self.samples_processed += count
        self._input_square_sum += float(np.sum(x * x, dtype=np.float64))
        self._output_square_sum += float(np.sum(output * output, dtype=np.float64))
        self._discriminator_sum_hz += float(np.sum(discriminator_hz, dtype=np.float64))
        self.input_peak = max(self.input_peak, float(np.max(np.abs(x))))
        self.output_peak = max(self.output_peak, float(np.max(np.abs(output))))
        self.input_clipping_count += int(np.count_nonzero(np.abs(x) >= 1.0))
        self.output_clipping_count += int(np.count_nonzero(np.abs(output) > 1.0))
        return output
