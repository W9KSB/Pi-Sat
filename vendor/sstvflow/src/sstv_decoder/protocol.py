"""Full calibration/VIS validation and duration-qualified sync candidates."""
from collections import deque
import numpy as np
from .modes import VIS_MODES


def decode_vis(frequencies, offset=0.0):
    """Seven LSB-first bits and even parity; reject frequencies between symbols."""
    f = np.asarray(frequencies) - offset
    if len(f) != 8 or np.any(np.minimum(abs(f-1100), abs(f-1300)) > 45):
        return None
    bits = f < 1200
    if int(np.sum(bits)) % 2:
        return None
    return sum(int(bits[i]) << i for i in range(7))


class ProtocolDetector:
    def __init__(self, rate, history, header_callback, sync_callback):
        self.rate = rate
        self.history = history
        self.header_callback = header_callback
        self.sync_callback = sync_callback
        self.step = max(1, round(rate * .001))
        self.next_bin = 0
        self.stage = "search"
        self.leader = deque(maxlen=320)
        self.leader_start = None
        self.offset = 0.0
        self.sync_start = None
        # Sync candidates last at most 23 ms. Keep tracking a longer tone's
        # start and frequency without retaining its entire sample-bin history.
        self.sync_values = deque(maxlen=32)
        self.search_offset = True
        self.sync_frequency = None
        self.start_bit = None
        self.recent = deque(maxlen=5)
        self.leader_good_end = None
        self.second_good_end = None

    def _tone(self, f, target, tolerance=65):
        return abs(f - target - self.offset) < tolerance

    def consume(self):
        h = self.history
        while self.next_bin + self.step + 1 < h.latest:
            start = self.next_bin
            self.next_bin += self.step
            try:
                values = h.interval(start, start + self.step)
                amps = h.interval(start, start + self.step, True)
            except ValueError:
                continue
            f = float(np.mean(values))
            self.recent.append(f)
            if len(self.recent) < 5:
                continue
            f = float(np.median(self.recent))
            start -= 2*self.step
            level = float(np.median(amps))
            if level < .0015 or not 600 < f < 3100:
                self._header(start, float("inf"))
                self._sync(start, None)
                continue
            self._header(start, f)
            self._sync(start, f)

    def _header(self, start, f):
        rate = self.rate
        if self.stage == "search":
            if abs(f - 1900) < 300:
                if self.leader_start is None:
                    self.leader_start = start
                self.leader.append(f)
                self.leader_good_end = start
            else:
                if (self.leader_start is not None and
                    start - self.leader_start >= .24 * rate and
                    abs(f - (np.median(self.leader) - 700)) < 65):
                    self.offset = float(np.median(self.leader) - 1900)
                    self.break_start = start
                    self.stage = "break"
                if self.stage == "break" or self.leader_good_end is None or start-self.leader_good_end > .006*rate:
                    self.leader_start = None
                    self.leader.clear()
        elif self.stage == "break":
            if self._tone(f, 1900) and .006*rate <= start-self.break_start <= .025*rate:
                self.second_start = start
                self.second_values = deque([f], maxlen=400)
                self.second_good_end = start
                self.stage = "leader2"
            elif start - self.break_start > .026 * rate:
                self.stage = "search"
        elif self.stage == "leader2":
            # A continuous leader tone must also expire; checking this only
            # after a tone change retains an unbounded, unusable header.
            if start-self.second_start > .365*rate:
                self.stage = "search"
                self.second_values.clear()
            elif self._tone(f, 1900):
                self.second_values.append(f)
                self.second_good_end = start
            elif self._tone(f, 1200) and .24*rate <= start-self.second_start <= .36*rate:
                self.offset = float(np.median(self.second_values) - 1900)
                self.start_bit = self._edge(start, 1550 + self.offset)
                self.stage = "vis"
            elif start-self.second_good_end > .006*rate:
                self.stage = "search"
        elif self.stage == "vis":
            if start - self.start_bit >= .298 * rate:
                # Central portions exclude bit-edge transients and validate start/stop.
                means = [np.median([np.mean(self.history.interval(self.start_bit+(i*.03+.008+j*.001)*rate,
                                                        self.start_bit+(i*.03+.009+j*.001)*rate)) for j in range(14)])
                         for i in range(10)]
                code = decode_vis(means[1:9], self.offset)
                valid = self._tone(means[0], 1200, 45) and self._tone(means[9], 1200, 45)
                if valid and code in VIS_MODES:
                    confidence = float(np.clip(1 - np.std(self.second_values) / 80, 0, 1))
                    self.header_callback(code, self.start_bit + .300 * rate, self.offset, confidence)
                self.stage = "search"

    def _edge(self, start, threshold, rising=False):
        try:
            positions = np.arange(start - 3*self.step, start + self.step)
            values = self.history.read(positions)
            window = max(3, round(self.rate*.0005) | 1)
            values = np.convolve(np.pad(values, (window//2,window//2),mode="edge"), np.ones(window)/window,mode="valid")
            crossings = np.flatnonzero((values[:-1] <= threshold) & (values[1:] > threshold)) if rising else np.flatnonzero((values[:-1] >= threshold) & (values[1:] < threshold))
            if len(crossings):
                i = crossings[-1]
                return float(positions[i] + (threshold-values[i]) / (values[i+1]-values[i]))
        except ValueError:
            pass
        return float(start)

    def _sync(self, start, f):
        if self.search_offset:
            is_sync = f is not None and (abs(f-self.sync_frequency)<85 if self.sync_start is not None else 950<f<1450)
        else:
            is_sync = f is not None and self._tone(f,1200,70)
        if is_sync:
            if self.sync_start is None:
                self.sync_start = float(start) if self.search_offset else self._edge(start, 1350 + self.offset)
                self.sync_values.clear()
                self.sync_frequency = f
            self.sync_values.append(f)
            if self.search_offset:
                self.sync_frequency=float(np.median(list(self.sync_values)[-5:]))
        elif self.sync_start is not None:
            duration = (start-self.sync_start) / self.rate
            if .002 <= duration <= .023:
                confidence = float(np.clip(1 - np.std(self.sync_values)/70, 0, 1))
                # The fixed sync-to-porch transition is independent of the previous
                # picture pixel. Use its midpoint rather than a content-dependent
                # falling threshold to timestamp sync. Period fitting handles drift.
                nominal = .004862 if duration < .006 else .009 if duration <= .013 else .020
                measured_offset=float(np.median(self.sync_values)-1200)
                end = self._edge(start, 1350+measured_offset, rising=True)
                self.sync_callback(end-nominal*self.rate, duration, measured_offset, confidence)
            self.sync_start = None
            self.sync_values.clear()
            self.sync_frequency = None
