"""Receive-only WebRTC audio transport for the native radio PCM fan-out."""

from __future__ import annotations

import asyncio
from collections import deque
from contextlib import suppress
from fractions import Fraction
import logging
from time import monotonic
from typing import Any, Callable

try:
    from aiortc import RTCConfiguration, RTCPeerConnection, RTCSessionDescription
    from aiortc.mediastreams import AudioStreamTrack, MediaStreamError
    from av import AudioFrame, AudioResampler

    IMPORT_ERROR: Exception | None = None
except Exception as exc:  # pragma: no cover - exercised by dependency probes
    RTCConfiguration = RTCPeerConnection = RTCSessionDescription = None
    AudioStreamTrack = object
    AudioFrame = AudioResampler = None
    MediaStreamError = RuntimeError
    IMPORT_ERROR = exc


LOGGER = logging.getLogger(__name__)
HEADER_BYTES = 24
OPUS_RATE = 48_000
FRAME_MS = 20
FRAME_SAMPLES = OPUS_RATE * FRAME_MS // 1000
FRAME_BYTES = FRAME_SAMPLES * 2 * 2
# Listening latency is bounded by how much resampled audio the track holds, so
# a burst that is never trimmed becomes permanent delay. Trim back to the target
# once the backlog passes the ceiling; the ceiling itself absorbs ordinary LAN
# batching. Both values are whole 20 ms output frames.
TARGET_BUFFER_MS = 40
CEILING_BUFFER_MS = 80


class WebrtcUnavailable(RuntimeError):
    """The optional WebRTC stack or a connected radio is unavailable."""


def availability() -> dict[str, object]:
    if IMPORT_ERROR is None:
        return {"available": True, "reason": None}
    return {"available": False, "reason": f"WebRTC audio is unavailable: {IMPORT_ERROR}"}


def stereo_payload(packet: bytes, channels: int) -> bytes | None:
    """Validate one native audio packet and return interleaved stereo PCM."""
    if channels not in (1, 2) or len(packet) < HEADER_BYTES:
        return None
    total = int.from_bytes(packet[0:4], "little")
    flags = int.from_bytes(packet[4:6], "little")
    declared = int.from_bytes(packet[22:24], "big")
    payload = packet[HEADER_BYTES:]
    if total != len(packet) or flags != 0 or declared != len(payload):
        return None
    if not payload or len(payload) % (2 * channels):
        return None
    if channels == 2:
        return payload
    mono = memoryview(payload).cast("h")
    stereo = bytearray(len(payload) * 2)
    out = memoryview(stereo).cast("h")
    for index, sample in enumerate(mono):
        out[index * 2] = sample
        out[index * 2 + 1] = sample
    return bytes(stereo)


def _frame_bytes(duration_ms: int) -> int:
    """Bytes held by whole output frames covering ``duration_ms``."""
    return FRAME_BYTES * max(1, duration_ms // FRAME_MS)


def backlog_trim_bytes(queued_bytes: int) -> int:
    """Bytes to discard from the head of the queue to restore the target backlog.

    Returns zero while the backlog is within the ceiling so ordinary jitter is
    still absorbed. The discarded amount is aligned to whole output frames, so
    the track never resumes mid-frame.
    """
    if queued_bytes <= _frame_bytes(CEILING_BUFFER_MS):
        return 0
    drop = queued_bytes - _frame_bytes(TARGET_BUFFER_MS)
    return drop - drop % FRAME_BYTES


if IMPORT_ERROR is None:

    class RadioAudioTrack(AudioStreamTrack):
        """Convert native PCM packets into paced, fixed-size 48 kHz frames."""

        kind = "audio"

        def __init__(self, controller: Any, get_controller: Callable[[], Any]):
            super().__init__()
            self._controller = controller
            self._get_controller = get_controller
            self._channels = 2 if controller.config.rx_codec == "lpcm16_stereo" else 1
            self._queue = controller.subscribe_audio()
            self._resampler = AudioResampler(
                format="s16",
                layout="stereo",
                rate=OPUS_RATE,
                frame_size=FRAME_SAMPLES,
            )
            self._audio = bytearray()
            self._input_pts = 0
            self._next_frame_at = monotonic()
            self._ended = False
            self._created_at = monotonic()
            self._first_audio_at: float | None = None
            self._frames_emitted = 0
            self._silent_frames = 0
            self._trimmed_frames = 0
            self._peak_backlog_ms = 0
            self._first_recv_logged = False

        async def _wait_for_slot(self) -> None:
            target = self._next_frame_at
            delay = target - monotonic()
            if delay > 0:
                await asyncio.sleep(delay)
            # Advance by exactly one RTP frame even if encoding or delivery ran
            # late. Resetting to ``now`` here would slowly stretch the stream.
            self._next_frame_at += FRAME_MS / 1000

        def _append_resampled(self, packet: bytes) -> None:
            payload = stereo_payload(packet, self._channels)
            if payload is None:
                return
            samples = len(payload) // 4
            frame = AudioFrame(format="s16", layout="stereo", samples=samples)
            frame.planes[0].update(payload)
            frame.sample_rate = self._controller.config.sample_rate
            frame.pts = self._input_pts
            frame.time_base = Fraction(1, frame.sample_rate)
            self._input_pts += samples
            if self._first_audio_at is None:
                # Start the pacing clock with the first real audio. Until the
                # radio produces audio the track emits silence, and letting the
                # deadline age across that gap would emit a catch-up burst.
                self._first_audio_at = monotonic()
                self._next_frame_at = self._first_audio_at
                # One line per session, so a connect delay can be attributed to
                # the radio feed rather than to the browser's negotiation.
                LOGGER.info(
                    "WebRTC RX audio started: first PCM %.2fs after the track was created",
                    self._first_audio_at - self._created_at,
                )
            for output in self._resampler.resample(frame):
                self._audio.extend(bytes(output.planes[0]))
            self._peak_backlog_ms = max(
                self._peak_backlog_ms, len(self._audio) // FRAME_BYTES * FRAME_MS
            )
            drop = backlog_trim_bytes(len(self._audio))
            if drop:
                del self._audio[:drop]
                self._trimmed_frames += drop // FRAME_BYTES

        def _fill(self) -> None:
            for packet in self._controller.read_audio(self._queue):
                self._append_resampled(packet)

        async def recv(self):
            if self._ended or self.readyState != "live":
                raise MediaStreamError
            if self._controller is not self._get_controller():
                self.end()
                raise MediaStreamError
            if not self._first_recv_logged:
                # Separates "the transport took seconds to carry media" from
                # "media flowed but the radio feed had not arrived yet".
                self._first_recv_logged = True
                LOGGER.debug(
                    "WebRTC RX audio track: sender started %.2fs after the track was created",
                    monotonic() - self._created_at,
                )
            await self._wait_for_slot()
            self._fill()
            payload = bytes(self._audio[:FRAME_BYTES])
            del self._audio[:FRAME_BYTES]
            if len(payload) < FRAME_BYTES:
                if not payload:
                    self._silent_frames += 1
                payload += bytes(FRAME_BYTES - len(payload))
            self._frames_emitted += 1
            frame = AudioFrame(format="s16", layout="stereo", samples=FRAME_SAMPLES)
            frame.planes[0].update(payload)
            frame.pts = getattr(self, "_timestamp", 0)
            frame.sample_rate = OPUS_RATE
            frame.time_base = Fraction(1, OPUS_RATE)
            self._timestamp = frame.pts + FRAME_SAMPLES
            return frame

        def stats(self) -> dict[str, object]:
            """Session counters for diagnosing listening latency."""
            first_audio = (
                None if self._first_audio_at is None
                else round(self._first_audio_at - self._created_at, 3)
            )
            return {
                "frames": self._frames_emitted,
                "silent_frames": self._silent_frames,
                "trimmed_frames": self._trimmed_frames,
                "buffered_ms": len(self._audio) // FRAME_BYTES * FRAME_MS,
                "peak_buffered_ms": self._peak_backlog_ms,
                "target_ms": TARGET_BUFFER_MS,
                "ceiling_ms": CEILING_BUFFER_MS,
                "first_audio_after_s": first_audio,
            }

        def end(self) -> None:
            if self._ended:
                return
            self._ended = True
            self.stop()
            self._controller.unsubscribe_audio(self._queue)
            if self._frames_emitted:
                counters = self.stats()
                LOGGER.info(
                    "WebRTC RX audio ended: frames=%s silent=%s trimmed=%s "
                    "peak_buffered_ms=%s first_audio_after_s=%s",
                    counters["frames"], counters["silent_frames"],
                    counters["trimmed_frames"], counters["peak_buffered_ms"],
                    counters["first_audio_after_s"],
                )


else:

    class RadioAudioTrack:  # pragma: no cover - dependency absence path
        def __init__(self, *_args, **_kwargs):
            raise WebrtcUnavailable(str(IMPORT_ERROR))


class WebrtcAudioTransport:
    """Own WebRTC peer connections so an HTTP offer does not end the media."""

    def __init__(self, *, get_controller: Callable[[], Any]):
        self._get_controller = get_controller
        self._peers: set[Any] = set()

    @staticmethod
    async def _wait_for_ice(peer: Any) -> None:
        if getattr(peer, "iceGatheringState", "complete") != "gathering":
            return
        finished = asyncio.Event()

        @peer.on("icegatheringstatechange")
        async def on_ice_gathering_state_change() -> None:
            if peer.iceGatheringState == "complete":
                finished.set()

        with suppress(asyncio.TimeoutError):
            await asyncio.wait_for(finished.wait(), timeout=5)

    async def handle_offer(self, payload: dict[str, Any]) -> dict[str, str]:
        if IMPORT_ERROR is not None:
            raise WebrtcUnavailable(str(IMPORT_ERROR))
        controller = self._get_controller()
        if controller is None:
            raise WebrtcUnavailable("Advanced Icom control is disabled.")
        sdp = str(payload.get("sdp") or "")
        offer_type = str(payload.get("type") or "")
        if not sdp or offer_type != "offer":
            raise ValueError("A WebRTC SDP offer is required.")

        # aiortc defaults to a public STUN server when none is configured, and
        # aioice blocks local candidate gathering for up to five seconds waiting
        # for every interface to answer it. On a station whose interfaces are
        # not all internet-routed, that delay lands before the SDP answer, so
        # listening audio does not start until it expires. Host candidates cover
        # every path the browser already uses to reach this host.
        peer = RTCPeerConnection(RTCConfiguration(iceServers=[]))
        started_at = monotonic()
        track = RadioAudioTrack(controller, self._get_controller)
        self._peers.add(peer)

        connected_logged = False

        @peer.on("iceconnectionstatechange")
        async def on_ice_connection_state_change() -> None:
            LOGGER.debug(
                "WebRTC ICE %s %.2fs after the audio track was created",
                peer.iceConnectionState, monotonic() - started_at,
            )

        @peer.on("connectionstatechange")
        async def on_connectionstatechange() -> None:
            nonlocal connected_logged
            if peer.connectionState == "connected" and not connected_logged:
                connected_logged = True
                LOGGER.debug(
                    "WebRTC peer connected %.2fs after the audio track was created",
                    monotonic() - started_at,
                )
            if peer.connectionState in {"failed", "closed", "disconnected"}:
                track.end()
                self._peers.discard(peer)
                with suppress(Exception):
                    await peer.close()

        try:
            await peer.setRemoteDescription(RTCSessionDescription(sdp=sdp, type=offer_type))
            with suppress(Exception):
                # DTLS shares ICE's transport, so its transitions separate a
                # slow handshake from slow connectivity checks.
                dtls_transport = peer.getTransceivers()[0].receiver.transport

                @dtls_transport.on("statechange")
                def on_dtls_state_change() -> None:
                    LOGGER.debug(
                        "WebRTC DTLS %s %.2fs after the audio track was created",
                        dtls_transport.state, monotonic() - started_at,
                    )

            peer.addTrack(track)
            answer = await peer.createAnswer()
            await peer.setLocalDescription(answer)
            LOGGER.debug(
                "WebRTC answer ready %.2fs after the audio track was created",
                monotonic() - started_at,
            )
            # Advertising a candidate the browser cannot reach costs an ICE
            # check timeout before the next pair is tried, so list what we offer.
            advertised = []
            for line in (peer.localDescription.sdp or "").splitlines():
                if not line.startswith("a=candidate:"):
                    continue
                parts = line.split()
                if len(parts) >= 8:
                    advertised.append(f"{parts[4]}:{parts[5]} {parts[7]}")
            LOGGER.debug("WebRTC local candidates: %s", ", ".join(advertised) or "none")
            await self._wait_for_ice(peer)
            return {"type": peer.localDescription.type, "sdp": peer.localDescription.sdp}
        except Exception:
            track.end()
            self._peers.discard(peer)
            with suppress(Exception):
                await peer.close()
            raise
