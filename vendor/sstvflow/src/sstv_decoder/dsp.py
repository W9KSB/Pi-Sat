"""Continuous FIR analytic signal; all positions refer to input sample time."""
import numpy as np
from scipy.signal import lfilter, firwin


class Demodulator:
    def __init__(self, rate, method="hilbert"):
        self.rate = rate
        self.method = method
        length = int(rate * .004) | 1
        self.delay = (length - 1) // 2
        n = np.arange(length) - self.delay
        if method == "hilbert":
            # Linear-phase band limit removes DC and out-of-band RF noise without
            # a voice AGC. Its exact delay is included in the sample timeline.
            self.bandpass = firwin(length, [400, min(6000, rate*.44)], fs=rate, pass_zero=False)
            imag = np.zeros(length)
            odd = n % 2 != 0
            imag[odd] = 2 / (np.pi * n[odd])
            imag *= np.kaiser(length, 5)
            self.kernel = 1j * imag
            self.kernel[self.delay] += 1
        elif method in ("quadrature", "narrow"):
            self.bandpass = None
            bandwidth=1450 if method=="quadrature" else 950
            self.kernel = 2 * firwin(length, bandwidth, fs=rate) * np.exp(2j * np.pi * 1900 * n / rate)
        else:
            raise ValueError("unknown demodulator")
        if self.bandpass is not None:
            self.delay *= 2
        self.reset()

    def reset(self):
        self.state = np.zeros(len(self.kernel) - 1, complex)
        self.band_state = np.zeros(len(self.kernel) - 1)
        self.previous = 0j

    def process(self, samples, start):
        if self.bandpass is not None:
            samples, self.band_state = lfilter(self.bandpass, [1], samples, zi=self.band_state)
        analytic, self.state = lfilter(self.kernel, [1], samples, zi=self.state)
        prior = np.r_[self.previous, analytic[:-1]]
        self.previous = analytic[-1] if len(analytic) else self.previous
        frequency = np.angle(analytic * prior.conj()) * self.rate / (2 * np.pi)
        amplitude = np.abs(analytic)
        positions = start + np.arange(len(samples)) - self.delay - .5
        return positions, frequency, amplitude


class FrequencyBuffer:
    """Bounded, contiguous sample-grid frequency history, with fractional reads."""
    def __init__(self, rate, seconds=2.5):
        self.capacity = int(rate * seconds)
        self.values = np.zeros(self.capacity)
        self.amplitudes = np.zeros(self.capacity)
        self.end = 0
        self.origin = 0.0

    def append(self, positions, frequency, amplitude):
        if not len(frequency):
            return
        if self.end == 0:
            self.origin = float(positions[0])
        index = (self.end + np.arange(len(frequency))) % self.capacity
        self.values[index] = frequency
        self.amplitudes[index] = amplitude
        self.end += len(frequency)

    @property
    def latest(self):
        return self.origin + self.end - 1

    def read(self, positions, amplitude=False):
        positions = np.asarray(positions)
        index = positions - self.origin
        lower = np.floor(index).astype(np.int64)
        if np.any(lower < max(0, self.end - self.capacity)) or np.any(lower + 1 >= self.end):
            raise ValueError("requested samples outside bounded frequency history")
        data = self.amplitudes if amplitude else self.values
        weight = index - lower
        return data[lower % self.capacity] * (1 - weight) + data[(lower + 1) % self.capacity] * weight

    def interval(self, start, end, amplitude=False):
        return self.read(np.arange(np.ceil(start), np.floor(end))) if not amplitude else self.read(np.arange(np.ceil(start), np.floor(end)), True)

    def read_correlation(self, positions, sample_rate):
        """Interpolate analytic phase products, never wrapped frequency angles.

        Frequency observations are angles of z[n] conj(z[n-1]). Interpolating
        their Hz values through a +/-Nyquist wrap manufactures a false tone.
        Across a wrap or amplitude null, interpolate complex products first.
        Preserve the established estimator on ordinary smooth segments.
        """
        index = np.asarray(positions) - self.origin
        lower = np.floor(index).astype(np.int64)
        if (np.any(lower - 1 < max(0, self.end - self.capacity))
                or np.any(lower + 1 >= self.end)):
            raise ValueError("requested samples outside bounded frequency history")
        upper = lower + 1
        amplitude = self.amplitudes[lower % self.capacity]
        previous = self.amplitudes[(lower - 1) % self.capacity]
        following = self.amplitudes[upper % self.capacity]
        left_frequency, right_frequency = self.values[lower % self.capacity], self.values[upper % self.capacity]
        weight = index - lower
        ordinary = (((1-weight)*amplitude + weight*following)
                    * ((1-weight)*previous + weight*amplitude)
                    * np.exp(2j*np.pi*((1-weight)*left_frequency + weight*right_frequency)/sample_rate))
        unreliable = ((abs(right_frequency-left_frequency) > sample_rate/2)
                      | (np.minimum(np.minimum(previous,amplitude),following)
                         < .1*np.maximum(np.maximum(previous,amplitude),following)))
        if np.any(unreliable):
            left = amplitude*previous*np.exp(2j*np.pi*left_frequency/sample_rate)
            right = following*amplitude*np.exp(2j*np.pi*right_frequency/sample_rate)
            ordinary = np.where(unreliable, left*(1-weight)+right*weight, ordinary)
        return ordinary
