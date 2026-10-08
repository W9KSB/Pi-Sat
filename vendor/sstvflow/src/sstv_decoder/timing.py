"""Robust global clock fit; single pulses do not independently position rows."""
from collections import deque
import numpy as np


class LineClock:
    def __init__(self, rate, start, period=.150):
        self.nominal = rate * period
        self.period = self.nominal
        self.origin = float(start)
        self.observations = deque(maxlen=64)
        self.confidence = 0.0
        self.auto = True
        self.manual_ppm = 0.0
        self.jitter = 0.0
        self.stable_fit = False
        self.phase_limit = rate * .000010
        self.jitter_limit = rate * .000100

    def predict(self, line):
        return self.origin + line * self.period

    def observe(self, line, position, confidence=1.0):
        if not all(np.isfinite(v) for v in (line, position, confidence)) or confidence <= .1:
            return False
        confidence = min(1.0, confidence)
        if self.stable_fit and confidence < .7:
            return False
        residual = position - self.predict(line)
        if self.observations and abs(residual) > self.nominal * .025:
            return False
        self.observations.append((line, position, confidence))
        if not self.auto:
            self.period = self.nominal * (1 + self.manual_ppm / 1e6)
            return True
        a = np.array(self.observations)
        if len(a) < 3:
            self.origin += .25 * residual
            return True
        x, y, weights = a.T
        center = np.average(x, weights=weights)
        mean = np.average(y, weights=weights)
        slope = np.sum(weights * (x-center) * (y-mean)) / np.sum(weights * (x-center)**2)
        intercept = mean - slope * center
        errors = y - (intercept + slope*x)
        keep = np.abs(errors) < max(self.nominal * .002, 3 * np.median(np.abs(errors)))
        if np.sum(keep) >= 3 and not np.all(keep):
            fit = np.polyfit(x[keep], y[keep], 1, w=np.sqrt(weights[keep]))
            slope, intercept = fit
        errors = y - (intercept + slope*x)
        self.jitter = float(np.sqrt(np.average(errors[keep]**2, weights=weights[keep])))
        fit_confidence = (min(1.0, len(a) / 12) * float(np.mean(keep))
                          * float(np.average(weights[keep]))
                          / (1 + (self.jitter / self.jitter_limit)**2))
        self.confidence = fit_confidence
        if abs(slope / self.nominal - 1) < .02:
            # A fade or erratic syncs must not pull an already consistent clock.
            # Keep collecting evidence so a later coherent clock change can fit.
            if self.stable_fit and fit_confidence < .5:
                return True
            # Smooth position at current line, then update period: no large row jump.
            target = intercept + slope * line
            correction = .35 * (target - self.predict(line))
            # Sustained, coherent evidence may reveal a genuine phase/clock
            # change. Let that fit reacquire rather than lag indefinitely.
            reacquiring = (self.jitter < self.jitter_limit * .25
                           and abs(target - self.predict(line)) > 4 * self.phase_limit)
            if self.stable_fit and not reacquiring:
                correction = np.clip(correction, -self.phase_limit, self.phase_limit)
            phase = self.predict(line) + correction
            self.period += .40 * (slope - self.period)
            self.origin = phase - line * self.period
            self.stable_fit = self.stable_fit or fit_confidence >= .7
        return True

    def status(self, line):
        return dict(estimated_line_period_samples=self.period,
                    sample_clock_error_ppm=(self.period / self.nominal - 1) * 1e6,
                    timing_confidence=self.confidence,
                    sync_jitter_samples=self.jitter,
                    timing_locked=self.confidence >= .5,
                    predicted_next_line_sample=self.predict(line),
                    line_start_phase_samples=self.origin)
