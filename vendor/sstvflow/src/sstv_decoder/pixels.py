"""Fit isolated raw PCM pixel windows without smoothing across pixel boundaries."""

import numpy as np
from scipy.ndimage import uniform_filter1d


def coherent_pixel_frequency(frequencies, amplitudes, previous_amplitudes, sample_rate):
    """Integrate adjacent analytic correlations before taking their phase.

    Near amplitude nulls the instantaneous phase is unreliable. The amplitude
    product gives those observations little leverage without widening a pixel.
    Arrays are (aperture samples, pixels); frequencies retain the PCM timeline.
    """
    weights = amplitudes * previous_amplitudes
    correlation = np.sum(weights * np.exp(2j * np.pi * frequencies / sample_rate), axis=0)
    return np.angle(correlation) * sample_rate / (2 * np.pi)


def _structured_detail(values):
    """Recognize locally recurring detail before using picture variance as noise.

    Correlation with shifted samples distinguishes coherent strokes/texture from
    independent noise. Use several lags because alternating pixels have negative
    correlation at one pixel but positive correlation at their period.
    """
    values = np.asarray(values, dtype=float)
    protected = np.zeros(values.shape, dtype=bool)
    if len(values) < 17:
        return protected
    padded = np.pad(values, 8, mode="edge")
    mean = uniform_filter1d(values, 17, mode="nearest")
    variance = np.maximum(0, uniform_filter1d(values**2, 17, mode="nearest") - mean**2)
    for lag in (*range(-8, 0), *range(1, 9)):
        shifted = padded[8 + lag:8 + lag + len(values)]
        other_mean = uniform_filter1d(shifted, 17, mode="nearest")
        other_variance = np.maximum(0, uniform_filter1d(shifted**2, 17, mode="nearest") - other_mean**2)
        covariance = uniform_filter1d(values * shifted, 17, mode="nearest") - mean * other_mean
        protected |= covariance > .6 * np.sqrt(np.maximum(variance * other_variance, 1e-12))
    return protected


def adaptive_channel_noise_reduction(frequencies, *, noise_ratio=None):
    """Local Wiener estimate: smooth noisy flat areas, retain stronger edges.

    Noise power is estimated only where there is no recurring picture detail.
    ``noise_ratio`` is optional independently measured reference-tone residual;
    clean references bypass filtering. It gates reception noise, rather than
    pretending to convert PCM residual into pixel-frequency variance. This does
    not need neighboring rows or additional audio latency.
    """
    frequencies = np.asarray(frequencies, dtype=float)
    if noise_ratio is not None and noise_ratio <= .02:
        return frequencies.copy()
    detail = _structured_detail(frequencies)
    if np.all(detail):
        return frequencies.copy()
    mean = uniform_filter1d(frequencies, 5, mode="nearest")
    variance = np.maximum(0, uniform_filter1d(frequencies**2, 5, mode="nearest") - mean**2)
    noise = float(np.median(variance[~detail]))
    gain = np.maximum(0, 1 - noise / np.maximum(variance, 1e-12))
    gain[detail] = 1
    return mean + gain * (frequencies - mean)


def fit_pixel_frequencies(samples, sample_rate, *, candidate_frequencies=None,
                          offset_hz=0.0, sample_times=None):
    """Return frequency, relative residual, and fit confidence for each window.

    ``samples`` is a finite real matrix shaped (pixels, samples), with at least
    three samples per window. Each window must lie entirely within one pixel's
    constant-frequency interval. Remove DC upstream, never by subtracting each
    short window's mean. ``sample_times`` optionally supplies relative times in
    seconds, shaped (samples,) or like ``samples``; the default is the PCM grid.

    The fit projects each window onto sine and cosine at each candidate, then
    refines the minimum using a parabola through the adjacent residuals. Default
    candidates are 1400..2400 Hz in 10 Hz increments, shifted by ``offset_hz``.
    Custom candidates are actual frequencies and are not shifted by the offset.
    Results at the candidate range boundaries remain on that boundary.

    Residual is unexplained signal energy divided by total signal energy;
    confidence is its complement. This measures sinusoidal fit, not frequency
    certainty: tiny windows can fit noise closely. Silence returns the middle
    candidate with residual 1 and confidence 0. Processing uses bounded batches.
    """
    if np.iscomplexobj(samples):
        raise ValueError("samples must be real PCM")
    samples = np.asarray(samples, dtype=np.float64)
    if samples.ndim != 2 or samples.shape[1] < 3 or not np.all(np.isfinite(samples)):
        raise ValueError("samples must be a finite matrix with at least three samples per window")
    if not np.isfinite(sample_rate) or sample_rate <= 0:
        raise ValueError("sample_rate must be finite and positive")
    if not np.isfinite(offset_hz):
        raise ValueError("offset_hz must be finite")
    frequencies = (np.arange(1400.0, 2400.1, 10.0) + offset_hz
                   if candidate_frequencies is None
                   else np.asarray(candidate_frequencies, dtype=np.float64))
    if (frequencies.ndim != 1 or len(frequencies) < 3
            or not np.all(np.isfinite(frequencies)) or np.any(np.diff(frequencies) <= 0)
            or frequencies[0] <= 0 or frequencies[-1] >= sample_rate / 2):
        raise ValueError("candidate frequencies must increase strictly within (0, Nyquist)")
    times = (np.arange(samples.shape[1], dtype=np.float64) / sample_rate
             if sample_times is None else np.asarray(sample_times, dtype=np.float64))
    if (times.shape not in ((samples.shape[1],), samples.shape)
            or not np.all(np.isfinite(times)) or np.any(np.diff(times, axis=-1) <= 0)):
        raise ValueError("sample_times must be finite increasing times shaped like each window or samples")
    # Centering improves phase arithmetic for caller-supplied absolute times.
    times = times - times[..., :1]
    result = np.empty(samples.shape[0])
    residual = np.empty(samples.shape[0])
    if times.ndim == 1:
        phase = 2 * np.pi * times[None, :] * frequencies[:, None]
        shared_cosine, shared_sine = np.cos(phase), np.sin(phase)
        shared_cc = np.sum(shared_cosine ** 2, axis=-1)
        shared_ss = np.sum(shared_sine ** 2, axis=-1)
        shared_cs = np.sum(shared_cosine * shared_sine, axis=-1)
    for first in range(0, len(samples), 256):
        last = min(first + 256, len(samples))
        windows = samples[first:last]
        window_times = times if times.ndim == 1 else times[first:last]
        if window_times.ndim == 1:
            cc, ss, cs = shared_cc, shared_ss, shared_cs
            yc = np.einsum("pn,fn->pf", windows, shared_cosine)
            ys = np.einsum("pn,fn->pf", windows, shared_sine)
            window_times = np.broadcast_to(window_times, windows.shape)
        else:
            phase = 2 * np.pi * window_times[:, None, :] * frequencies[None, :, None]
            cosine, sine = np.cos(phase), np.sin(phase)
            cc = np.sum(cosine * cosine, axis=-1)
            ss = np.sum(sine * sine, axis=-1)
            cs = np.sum(cosine * sine, axis=-1)
            yc = np.sum(windows[:, None, :] * cosine, axis=-1)
            ys = np.sum(windows[:, None, :] * sine, axis=-1)
        determinant = cc * ss - cs * cs
        valid = determinant > np.finfo(float).eps * cc * ss * 32
        denominator = np.where(valid, determinant, 1.0)
        a = (yc * ss - ys * cs) / denominator
        b = (ys * cc - yc * cs) / denominator
        # The coarse projection avoids materializing (pixels, candidates,
        # samples) residuals. Reevaluate the final residual directly below.
        energy = np.sum(windows ** 2, axis=1)
        errors = energy[:, None] - a * yc - b * ys
        errors = np.where(valid, errors, np.inf)
        indices = np.argmin(errors, axis=1)
        rows = np.arange(len(windows))
        chosen = frequencies[indices].copy()
        interior = (indices > 0) & (indices < len(frequencies) - 1)
        left = np.maximum(indices - 1, 0)
        right = np.minimum(indices + 1, len(frequencies) - 1)
        # Nonuniform candidate grids use the divided-difference parabola.
        dl = chosen - frequencies[left]
        dr = frequencies[right] - chosen
        safe_dl, safe_dr = np.where(interior, dl, 1), np.where(interior, dr, 1)
        sl = (errors[rows, indices] - errors[rows, left]) / safe_dl
        sr = (errors[rows, right] - errors[rows, indices]) / safe_dr
        curvature = (sr - sl) / (safe_dl + safe_dr)
        derivative = sl + curvature * safe_dl
        refined = np.zeros(len(windows))
        usable = interior & np.isfinite(curvature) & (curvature > 0)
        np.divide(-derivative, 2 * curvature, out=refined, where=usable)
        chosen += np.clip(refined, -dl, dr)
        # Evaluate the actual refined fit, avoiding cancellation in E - projection.
        phase = 2 * np.pi * chosen[:, None] * window_times
        cosine, sine = np.cos(phase), np.sin(phase)
        cc, ss = np.sum(cosine ** 2, axis=1), np.sum(sine ** 2, axis=1)
        cs = np.sum(cosine * sine, axis=1)
        yc, ys = np.sum(windows * cosine, axis=1), np.sum(windows * sine, axis=1)
        determinant = cc * ss - cs ** 2
        valid = determinant > np.finfo(float).eps * cc * ss * 32
        denominator = np.where(valid, determinant, 1.0)
        a, b = (yc * ss - ys * cs) / denominator, (ys * cc - yc * cs) / denominator
        energy = np.sum(windows ** 2, axis=1)
        error = np.sum((windows - a[:, None] * cosine - b[:, None] * sine) ** 2, axis=1)
        reliable = valid & (energy > np.finfo(float).tiny)
        relative = np.ones(len(windows))
        np.divide(error, energy, out=relative, where=reliable)
        relative = np.clip(relative, 0, 1)
        chosen[~reliable] = frequencies[len(frequencies) // 2]
        result[first:last], residual[first:last] = chosen, relative
    return result, residual, 1 - residual
