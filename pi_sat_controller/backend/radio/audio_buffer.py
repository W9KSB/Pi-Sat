"""Small, independently locked live-audio buffers, never protected by CAT locks."""
from collections import deque
from threading import Lock
from time import monotonic
from typing import Callable


class AudioBuffer:
    def __init__(
        self, sample_rate: int, channels: int,
        notify: Callable[[], None] | None = None,
        *, max_seconds: float = 0.12, drop_oldest: bool = True,
    ):
        self._lock = Lock()
        self._packets: deque[tuple[float, bytes, int]] = deque()
        self._frames = 0
        self._max_frames = max(1, int(sample_rate * max_seconds))
        self._max_seconds = max_seconds
        self._drop_oldest = drop_oldest
        self._channels = channels
        self._notify = notify
        self.dropped = 0
        self._overrun = False

    @property
    def overrun(self) -> bool:
        with self._lock:
            return self._overrun

    def append(self, packet: bytes) -> None:
        frames = max(1, (len(packet) - 24) // (2 * self._channels))
        now = monotonic()
        with self._lock:
            if not self._drop_oldest and (self._overrun or self._frames + frames > self._max_frames):
                self._overrun = True
                self.dropped += 1
                wake = True
            else:
                wake = not self._packets
                self._packets.append((now, packet, frames))
                self._frames += frames
                if self._drop_oldest:
                    while self._packets and (self._frames > self._max_frames or now - self._packets[0][0] > self._max_seconds):
                        self._frames -= self._packets.popleft()[2]
                        self.dropped += 1
        if wake and self._notify:
            self._notify()

    def drain(self) -> list[bytes]:
        now = monotonic()
        with self._lock:
            packets = []
            for timestamp, packet, _ in self._packets:
                if not self._drop_oldest or now - timestamp <= self._max_seconds:
                    packets.append(packet)
                else:
                    self.dropped += 1
            self._packets.clear()
            self._frames = 0
            return packets

    def clear(self) -> None:
        with self._lock:
            self._packets.clear()
            self._frames = 0
            self._overrun = False
