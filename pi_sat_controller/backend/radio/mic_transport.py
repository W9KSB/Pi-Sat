"""Bounded microphone transport and freshness validation."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
import logging
import math
import struct
from threading import Condition, Event, Thread
from time import monotonic
from typing import Callable

LOGGER = logging.getLogger("rx.audio_transport")

MIC_MAGIC = b"PISATMIC"
MIC_VERSION = 1
MIC_HEADER_BYTES = 64
MIC_MAX_AGE_S = 0.100
MIC_FUTURE_TOLERANCE_S = 0.050
MIC_MAX_CLOCK_UNCERTAINTY_S = 0.125
MIC_MAILBOX_FRAMES = 3
MIC_FRAME_INTERVAL_S = 0.020
MIC_DIAGNOSTIC_INTERVAL_S = 5.0

_MIC_HEADER = struct.Struct("<8sBBHIIII d f I I f f H H")


class MalformedMicFrame(ValueError):
    """A message used the microphone magic but did not form a valid frame."""


@dataclass(frozen=True)
class MicFrame:
    transport_id: int
    tx_generation: int
    capture_generation: int
    sequence: int
    capture_started_monotonic: float
    clock_uncertainty_s: float
    sample_rate: int
    frame_count: int
    peak: float
    rms: float
    clipped: int
    pcm: bytes


@dataclass(frozen=True)
class LegacyMicFrame:
    pcm: bytes
    received_at: float
    tx_generation: int


def parse_mic_message(data: bytes) -> MicFrame | LegacyMicFrame:
    """Parse an app envelope, retaining raw PCM as a compatibility path."""
    if not isinstance(data, (bytes, bytearray, memoryview)):
        raise MalformedMicFrame("Microphone messages must contain bytes")
    payload = bytes(data)
    if not payload:
        raise MalformedMicFrame("Microphone message is empty")
    if payload[:8] != MIC_MAGIC:
        return LegacyMicFrame(pcm=payload, received_at=monotonic(), tx_generation=0)
    if len(payload) < MIC_HEADER_BYTES:
        raise MalformedMicFrame("Microphone envelope is truncated")
    (
        magic,
        version,
        message_type,
        header_bytes,
        transport_id,
        tx_generation,
        capture_generation,
        sequence,
        capture_started,
        clock_uncertainty,
        sample_rate,
        frame_count,
        peak,
        rms,
        clipped,
        _reserved,
    ) = _MIC_HEADER.unpack_from(payload)
    if magic != MIC_MAGIC or version != MIC_VERSION or message_type != 1:
        raise MalformedMicFrame("Unsupported microphone envelope")
    if header_bytes != MIC_HEADER_BYTES:
        raise MalformedMicFrame("Unsupported microphone header size")
    pcm = payload[MIC_HEADER_BYTES:]
    if not pcm or len(pcm) % 2 or frame_count * 2 != len(pcm):
        raise MalformedMicFrame("Microphone PCM length does not match its header")
    if (
        transport_id <= 0
        or tx_generation <= 0
        or sample_rate < 8000
        or sample_rate > 96000
        or not math.isfinite(capture_started)
        or not math.isfinite(clock_uncertainty)
        or not math.isfinite(peak)
        or not math.isfinite(rms)
        or not 0.0 <= peak <= 1.0
        or not 0.0 <= rms <= 1.0
        or clipped > frame_count
    ):
        raise MalformedMicFrame("Microphone envelope contains invalid values")
    return MicFrame(
        transport_id=transport_id,
        tx_generation=tx_generation,
        capture_generation=capture_generation,
        sequence=sequence,
        capture_started_monotonic=capture_started / 1000.0,
        clock_uncertainty_s=clock_uncertainty / 1000.0,
        sample_rate=sample_rate,
        frame_count=frame_count,
        peak=peak,
        rms=rms,
        clipped=clipped,
        pcm=pcm,
    )


class MicrophoneDiagnostics:
    """Aggregate one microphone session without retaining payloads."""

    def __init__(self, *, debug_enabled: Callable[[], bool]) -> None:
        self._debug_enabled = debug_enabled
        self._started_at = monotonic()
        self._last_report_at = self._started_at
        self._counts: dict[str, int] = {}
        self._age_sum_s = 0.0
        self._age_max_s = 0.0
        self._gap_sum_s = 0.0
        self._gap_max_s = 0.0
        self._gap_count = 0
        self._peak = 0.0
        self._rms = 0.0
        self._clipped = 0
        self._client: dict[str, int | float] = {}

    def _bump(self, name: str, amount: int = 1) -> None:
        self._counts[name] = self._counts.get(name, 0) + amount

    def received(self, frame: MicFrame, *, received_at: float, age_s: float, gap_s: float | None) -> None:
        self._bump("received")
        self._age_sum_s += max(0.0, age_s)
        self._age_max_s = max(self._age_max_s, age_s)
        if gap_s is not None:
            self._gap_sum_s += max(0.0, gap_s)
            self._gap_max_s = max(self._gap_max_s, gap_s)
            self._gap_count += 1
        self._peak = max(self._peak, frame.peak)
        self._rms = max(self._rms, frame.rms)
        self._clipped += max(0, frame.clipped)
        self.maybe_emit(received_at)

    def legacy(self) -> None:
        self._bump("legacy")

    def sent(self) -> None:
        self._bump("sent")

    def drop(self, reason: str) -> None:
        self._bump("dropped")
        self._bump(f"drop_{reason}")

    def gap(self) -> None:
        self._bump("sequence_gap")

    def update_client(self, values: dict) -> None:
        allowed = (
            "captured", "sent", "dropped", "noPtt", "noClock", "stale",
            "backpressure", "legacy", "clipped", "peak", "rms", "idle",
        )
        for key in allowed:
            value = values.get(key)
            if isinstance(value, (int, float)) and math.isfinite(float(value)) and value >= 0:
                self._client[key] = value

    def maybe_emit(self, now: float, *, transport_diagnostics: Callable[[], dict] | None = None) -> None:
        if now - self._last_report_at < MIC_DIAGNOSTIC_INTERVAL_S:
            return
        self._last_report_at = now
        try:
            enabled = bool(self._debug_enabled())
        except Exception:
            enabled = False
        if not enabled:
            return
        counts = self._counts
        received = counts.get("received", 0)
        age_avg = self._age_sum_s / received * 1000 if received else 0.0
        gap_avg = self._gap_sum_s / self._gap_count * 1000 if self._gap_count else 0.0
        tx = transport_diagnostics() if transport_diagnostics else {}
        LOGGER.info(
            "RX audio role=rx mic summary received=%d sent=%d dropped=%d stale=%d "
            "clock=%d sequence_gaps=%d legacy=%d overflow=%d age_avg_ms=%.1f "
            "age_max_ms=%.1f socket_gap_avg_ms=%.1f socket_gap_max_ms=%.1f "
            "client_captured=%d client_sent=%d client_dropped=%d client_idle=%d "
            "peak=%.3f rms=%.3f clipped=%d tx_write_max_ms=%s "
            "tx_lock_wait_max_ms=%s tx_udp_spacing_min_ms=%s "
            "tx_udp_spacing_max_ms=%s frame_write_call_spacing_min_ms=%s "
            "frame_write_call_spacing_max_ms=%s",
            received,
            counts.get("sent", 0),
            counts.get("dropped", 0),
            counts.get("drop_stale", 0),
            counts.get("drop_clock", 0),
            counts.get("sequence_gap", 0),
            counts.get("legacy", 0),
            counts.get("drop_overflow", 0),
            age_avg,
            self._age_max_s * 1000,
            gap_avg,
            self._gap_max_s * 1000,
            int(self._client.get("captured", 0)),
            int(self._client.get("sent", 0)),
            int(self._client.get("dropped", 0)),
            int(self._client.get("idle", 0)),
            self._peak,
            self._rms,
            self._clipped,
            tx.get("tx_audio_write_max_ms", "unknown"),
            tx.get("tx_audio_lock_wait_max_ms", "unknown"),
            tx.get("tx_audio_udp_spacing_min_ms", "unknown"),
            tx.get("tx_audio_udp_spacing_max_ms", "unknown"),
            tx.get("tx_audio_write_call_spacing_min_ms", "unknown"),
            tx.get("tx_audio_write_call_spacing_max_ms", "unknown"),
        )


class MicrophoneSessionValidator:
    """Check envelope identity, ordering and mapped capture age."""

    def __init__(self, *, transport_id: int, sample_rate: int) -> None:
        self.transport_id = transport_id
        self.sample_rate = sample_rate
        self._last_sequence: dict[int, int] = {}
        self._last_received_at: float | None = None

    def validate(
        self,
        frame: MicFrame,
        *,
        received_at: float,
    ) -> tuple[float, float | None, bool] | str:
        if frame.transport_id != self.transport_id:
            return "transport"
        if frame.sample_rate != self.sample_rate:
            return "rate"
        expected_frames = max(1, round(self.sample_rate / 50))
        if frame.frame_count != expected_frames or len(frame.pcm) != frame.frame_count * 2:
            return "frame_size"
        if not 0 <= frame.clock_uncertainty_s <= MIC_MAX_CLOCK_UNCERTAINTY_S:
            return "clock"
        apparent_age = received_at - frame.capture_started_monotonic
        if apparent_age < -MIC_FUTURE_TOLERANCE_S:
            return "future"
        if apparent_age + frame.clock_uncertainty_s > MIC_MAX_AGE_S:
            return "stale"
        previous = self._last_sequence.get(frame.capture_generation)
        if previous is not None and frame.sequence <= previous:
            return "sequence"
        sequence_gap = previous is not None and frame.sequence > previous + 1
        self._last_sequence[frame.capture_generation] = frame.sequence
        gap = None
        if self._last_received_at is not None:
            gap = received_at - self._last_received_at
        self._last_received_at = received_at
        return apparent_age, gap, sequence_gap


class MicrophoneMailbox:
    """A small real-time mailbox with one dedicated transport thread.

    The radio consumes transmit PCM as a continuous 20 ms stream. Browser
    capture is normally paced by the audio clock, but a busy worker or WebSocket
    can deliver several frames together. Forwarding those frames immediately
    turns that jitter into an audio burst followed by a gap, so this mailbox
    retains at most three frames and releases one frame per radio interval.
    """

    def __init__(
        self,
        *,
        controller,
        diagnostics: MicrophoneDiagnostics,
        on_error: Callable[[Exception], None],
        is_current: Callable[[], bool],
    ) -> None:
        self._controller = controller
        self._diagnostics = diagnostics
        self._on_error = on_error
        self._is_current = is_current
        self._condition = Condition()
        self._frames: deque[tuple[MicFrame | LegacyMicFrame, float]] = deque()
        self._stopped = False
        self._thread = Thread(
            target=self._run,
            name="icom-microphone-transport",
            daemon=True,
        )

    def start(self) -> None:
        self._thread.start()

    def submit(self, frame: MicFrame | LegacyMicFrame) -> None:
        with self._condition:
            if self._stopped:
                self._diagnostics.drop("stopped")
                return
            if len(self._frames) >= MIC_MAILBOX_FRAMES:
                self._frames.popleft()
                self._diagnostics.drop("overflow")
            received_at = (
                frame.received_at
                if isinstance(frame, LegacyMicFrame)
                else monotonic()
            )
            self._frames.append((frame, received_at))
            self._condition.notify()

    def stop(self, timeout_s: float = 1.0) -> None:
        with self._condition:
            self._stopped = True
            self._frames.clear()
            self._condition.notify_all()
        if self._thread.is_alive():
            self._thread.join(timeout=max(0.0, timeout_s))

    def _run(self) -> None:
        pending: tuple[MicFrame | LegacyMicFrame, float] | None = None
        next_send_at: float | None = None
        while True:
            with self._condition:
                while True:
                    if self._stopped:
                        return
                    now = monotonic()
                    if pending is None:
                        if not self._frames:
                            self._condition.wait()
                            continue
                        pending = self._frames.popleft()
                        if next_send_at is None:
                            # Start immediately; the radio has already been
                            # primed with silence when the key was confirmed.
                            next_send_at = now
                    remaining = next_send_at - now
                    if remaining <= 0:
                        break
                    self._condition.wait(timeout=remaining)
                frame, received_at = pending
                pending = None
                # Re-anchor after the send so a long stall cannot create a
                # catch-up burst of queued microphone audio.
                next_send_at = monotonic() + MIC_FRAME_INTERVAL_S
            if not self._is_current():
                self._diagnostics.drop("session_changed")
                return
            if monotonic() - received_at > MIC_MAX_AGE_S:
                self._diagnostics.drop("stale")
                continue
            try:
                if isinstance(frame, LegacyMicFrame):
                    delivered = self._controller.send_microphone_audio(
                        frame.pcm,
                        tx_generation=frame.tx_generation,
                        received_monotonic=frame.received_at,
                    )
                else:
                    delivered = self._controller.send_microphone_audio(
                        frame.pcm,
                        transport_id=frame.transport_id,
                        tx_generation=frame.tx_generation,
                        capture_generation=frame.capture_generation,
                        capture_started_monotonic=frame.capture_started_monotonic,
                        clock_uncertainty_s=frame.clock_uncertainty_s,
                    )
            except Exception as exc:
                self._diagnostics.drop("exception")
                self._on_error(exc)
            else:
                if delivered:
                    self._diagnostics.sent()
                else:
                    self._diagnostics.drop("ptt")
