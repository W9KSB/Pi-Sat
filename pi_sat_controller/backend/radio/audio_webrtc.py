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
    from aiortc import RTCPeerConnection, RTCSessionDescription
    from aiortc.mediastreams import AudioStreamTrack, MediaStreamError
    from av import AudioFrame, AudioResampler

    IMPORT_ERROR: Exception | None = None
except Exception as exc:  # pragma: no cover - exercised by dependency probes
    RTCPeerConnection = RTCSessionDescription = None
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
MAX_BUFFER_MS = 120


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
            for output in self._resampler.resample(frame):
                self._audio.extend(bytes(output.planes[0]))
            maximum = FRAME_BYTES * (MAX_BUFFER_MS // FRAME_MS)
            if len(self._audio) > maximum:
                del self._audio[: len(self._audio) - maximum]

        def _fill(self) -> None:
            for packet in self._controller.read_audio(self._queue):
                self._append_resampled(packet)

        async def recv(self):
            if self._ended or self.readyState != "live":
                raise MediaStreamError
            if self._controller is not self._get_controller():
                self.end()
                raise MediaStreamError
            await self._wait_for_slot()
            self._fill()
            payload = bytes(self._audio[:FRAME_BYTES])
            del self._audio[:FRAME_BYTES]
            if len(payload) < FRAME_BYTES:
                payload += bytes(FRAME_BYTES - len(payload))
            frame = AudioFrame(format="s16", layout="stereo", samples=FRAME_SAMPLES)
            frame.planes[0].update(payload)
            frame.pts = getattr(self, "_timestamp", 0)
            frame.sample_rate = OPUS_RATE
            frame.time_base = Fraction(1, OPUS_RATE)
            self._timestamp = frame.pts + FRAME_SAMPLES
            return frame

        def end(self) -> None:
            if self._ended:
                return
            self._ended = True
            self.stop()
            self._controller.unsubscribe_audio(self._queue)


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

        peer = RTCPeerConnection()
        track = RadioAudioTrack(controller, self._get_controller)
        self._peers.add(peer)

        @peer.on("connectionstatechange")
        async def on_connectionstatechange() -> None:
            if peer.connectionState in {"failed", "closed", "disconnected"}:
                track.end()
                self._peers.discard(peer)
                with suppress(Exception):
                    await peer.close()

        try:
            await peer.setRemoteDescription(RTCSessionDescription(sdp=sdp, type=offer_type))
            peer.addTrack(track)
            answer = await peer.createAnswer()
            await peer.setLocalDescription(answer)
            await self._wait_for_ice(peer)
            return {"type": peer.localDescription.type, "sdp": peer.localDescription.sdp}
        except Exception:
            track.end()
            self._peers.discard(peer)
            with suppress(Exception):
                await peer.close()
            raise
