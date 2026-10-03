from __future__ import annotations

from array import array
import base64
from collections import deque
from collections.abc import Callable
import io
import json
import logging
import math
import os
from pathlib import Path
from queue import Empty, Full, Queue
import shutil
import subprocess
import sys
import wave
from threading import Event, Lock, RLock, Thread
from time import monotonic
from typing import Any

from pi_sat_controller.backend.audio_gain import apply_gain_db, clamp_gain_db, dbfs_of, peak_of
from pi_sat_controller.backend.sstv.gallery import SstvGallery, encode_png


LOGGER = logging.getLogger(__name__)

_AUDIO_SEQUENCE_OFFSET = 18
_MINOR_GAP_PACKETS = 3
_MAX_REASONABLE_GAP_PACKETS = 0x7FFF
_SSTV_AUDIO_BUFFER_SECONDS = 0.5
_DECODER_CAPTURE_SECONDS = 180
_FILE_SAMPLE_RATE = 16_000
_FILE_DECODE_TIMEOUT_S = 180.0
_MAX_FILE_PCM_BYTES = 128 * 1024 * 1024


class SstvDecodeError(RuntimeError):
    """Raised when uploaded audio cannot produce a complete or partial image."""


class SstvManager:
    """Owns one slowrx worker fed from a copy of native Icom RX PCM."""

    DISABLED = "Disabled"
    WAITING = "Waiting for radio audio"
    LISTENING = "Listening"
    DETECTED = "SSTV detected"
    DECODING = "Decoding"
    COMPLETE = "Decode complete"
    FAILED = "Decode failed"

    def __init__(
        self,
        *,
        project_root: Path,
        data_dir: Path,
        get_controller: Callable[[], Any],
        get_context: Callable[[], dict[str, Any]],
        rx_gain_db: float = -6.0,
    ) -> None:
        self.project_root = Path(project_root)
        self.gallery = SstvGallery(data_dir, retention=25)
        self.get_controller = get_controller
        self.get_context = get_context
        self._lock = RLock()
        self._lifecycle_lock = Lock()
        self._process_lock = Lock()
        self._enabled = False
        self._state = self.DISABLED
        self._error: str | None = None
        self._mode: str | None = None
        self._line = 0
        self._total_lines = 0
        self._frequency_offset_hz: float | None = None
        self._input_samples = 0
        self._audio_capture: deque[bytes] = deque()
        self._audio_capture_bytes = 0
        self._audio_capture_rate = 0
        # Level of the trimmed stream actually handed to the decoder.
        self._decoder_peak = 0
        self._rx_gain_db = clamp_gain_db(rx_gain_db)
        self._last_audio_at: float | None = None
        self._terminal_until = 0.0
        self._current: dict[str, Any] | None = None
        self._capture_context: dict[str, Any] = {}
        self._last_packet_sequence: int | None = None
        self._dropped_packets = 0
        self._interference_packets = 0
        self._subscribers: list[Queue[dict[str, Any]]] = []
        self._sequence = 0
        self._gallery_count = len(self.gallery.list())
        self._stop = Event()
        self._wake = Event()
        self._thread: Thread | None = None
        self._process: subprocess.Popen[bytes] | None = None
        self._process_generation = 0
        self._fatal_worker = False
        self._last_worker_error: str | None = None
        self._file_decode_lock = Lock()
        self._file_decode_state = "idle"
        self._file_decode_error: str | None = None

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            current = None
            if self._current is not None:
                current = {
                    key: self._current.get(key)
                    for key in ("mode", "width", "height", "last_line")
                }
            return {
                "enabled": self._enabled,
                "state": self._state,
                "error": self._error,
                "mode": self._mode,
                "line": self._line,
                "total_lines": self._total_lines,
                "progress_percent": (
                    round(100.0 * self._line / self._total_lines, 1)
                    if self._total_lines
                    else 0.0
                ),
                "frequency_offset_hz": self._frequency_offset_hz,
                "input_samples": self._input_samples,
                "rx_gain_db": self._rx_gain_db,
                "decoder_level_dbfs": dbfs_of(self._decoder_peak),
                "image_quality": self._current.get("quality") if self._current else None,
                "interference_packets": self._interference_packets,
                "dropped_packets": self._dropped_packets,
                "gallery_count": self._gallery_count,
                "audio_source": "Native IC-9700 SUB/RX PCM",
                "decoder": "slowrx.rs 0.5.3-pisat.3",
                "decoder_error": self._last_worker_error,
                "current_image": current,
                "file_decode": {
                    "state": self._file_decode_state,
                    "error": self._file_decode_error,
                },
                "sequence": self._sequence,
            }

    @staticmethod
    def compare_progressive_to_final(
        progressive: bytearray | None,
        received_lines: set[int],
        final_rgb: bytes,
        width: int,
        height: int,
    ) -> dict[str, int | str]:
        """Describe changes between rendered lines and slowrx's final image."""
        if progressive is None or len(progressive) != width * height * 3:
            return {
                "schema_version": 1,
                "received_lines": 0,
                "missing_lines": height,
                "changed_pixels": 0,
                "changed_channels": 0,
                "right_edge_changed_pixels": 0,
                "status": "unavailable",
            }
        changed_pixels = 0
        changed_channels = 0
        right_edge_changed_pixels = 0
        for line in received_lines:
            if not 0 <= line < height:
                continue
            start = line * width * 3
            for pixel in range(width):
                offset = start + pixel * 3
                before = progressive[offset : offset + 3]
                after = final_rgb[offset : offset + 3]
                differences = sum(left != right for left, right in zip(before, after))
                if differences:
                    changed_pixels += 1
                    changed_channels += differences
                    if pixel == width - 1:
                        right_edge_changed_pixels += 1
        return {
            "schema_version": 1,
            "received_lines": len(received_lines),
            "missing_lines": max(0, height - len(received_lines)),
            "changed_pixels": changed_pixels,
            "changed_channels": changed_channels,
            "right_edge_changed_pixels": right_edge_changed_pixels,
            "status": "matched" if changed_pixels == 0 else "changed",
        }

    def decode_uploaded_file(self, source_path: Path) -> dict[str, Any]:
        """Decode one temporary MP3/WAV file without touching the live worker."""
        if not isinstance(source_path, Path):
            source_path = Path(source_path)
        if not source_path.is_file():
            raise SstvDecodeError("The uploaded audio file is unavailable")
        with self._file_decode_lock:
            with self._lock:
                self._file_decode_state = "decoding"
                self._file_decode_error = None
            self._publish({"type": "file_decode_started"})
            try:
                result = self._decode_uploaded_file(source_path)
            except Exception as exc:
                message = exc.args[0] if exc.args else "Uploaded SSTV decode failed"
                message = str(message)
                with self._lock:
                    self._file_decode_state = "failed"
                    self._file_decode_error = message
                self._publish({"type": "file_decode_failed", "error": message})
                if isinstance(exc, SstvDecodeError):
                    raise
                raise SstvDecodeError(message) from exc
            with self._lock:
                self._file_decode_state = "complete"
                self._file_decode_error = None
            self._publish({"type": "file_decode_complete", "image": result})
            return result

    def _decode_uploaded_file(self, source_path: Path) -> dict[str, Any]:
        ffmpeg = shutil.which("ffmpeg")
        if ffmpeg is None:
            raise SstvDecodeError("ffmpeg is required for MP3/WAV upload decoding")
        decoder = self._decoder_path()
        if decoder is None:
            raise SstvDecodeError("The bundled 64-bit SSTV decoder is unavailable")
        convert: subprocess.Popen[bytes] | None = None
        try:
            convert = subprocess.Popen(
                [
                    ffmpeg,
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-i",
                    str(source_path),
                    "-vn",
                    "-f",
                    "s16le",
                    "-acodec",
                    "pcm_s16le",
                    "-ac",
                    "1",
                    "-ar",
                    str(_FILE_SAMPLE_RATE),
                    "pipe:1",
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                shell=False,
            )
            pcm, _ = convert.communicate(timeout=_FILE_DECODE_TIMEOUT_S)
        except subprocess.TimeoutExpired as exc:
            if convert is not None:
                convert.kill()
                convert.communicate()
            raise SstvDecodeError("Audio conversion timed out") from exc
        except OSError as exc:
            raise SstvDecodeError("Unable to start ffmpeg for audio conversion") from exc
        if convert is None or convert.returncode != 0 or not pcm:
            raise SstvDecodeError("The uploaded audio could not be converted to PCM")
        if len(pcm) > _MAX_FILE_PCM_BYTES:
            raise SstvDecodeError("The converted audio file is too large to decode")

        process: subprocess.Popen[bytes] | None = None
        try:
            process = subprocess.Popen(
                [str(decoder), str(_FILE_SAMPLE_RATE)],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                bufsize=0,
                shell=False,
            )
            stdout, _ = process.communicate(input=pcm, timeout=_FILE_DECODE_TIMEOUT_S)
        except subprocess.TimeoutExpired as exc:
            if process is not None:
                process.kill()
                process.communicate()
            raise SstvDecodeError("SSTV decoding timed out") from exc
        except OSError as exc:
            raise SstvDecodeError("Unable to start the bundled SSTV decoder") from exc
        if process is None or process.returncode != 0:
            raise SstvDecodeError("The SSTV decoder rejected the uploaded audio")

        progressive: bytearray | None = None
        received_lines: set[int] = set()
        mode: str | None = None
        width = 0
        height = 0
        final_rgb: bytes | None = None
        partial = False
        acquisition: dict[str, Any] | None = None
        for raw_line in stdout.splitlines():
            try:
                event = json.loads(raw_line)
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise SstvDecodeError("The SSTV decoder returned invalid event data") from exc
            if not isinstance(event, dict):
                continue
            event_type = event.get("type")
            if event_type in {"vis_detected", "blind_detected"}:
                mode = str(event.get("mode") or "SSTV")
                width = int(event["width"])
                height = int(event["height"])
                if not 0 < width <= 2048 or not 0 < height <= 2048:
                    raise SstvDecodeError("The decoder returned invalid image dimensions")
                progressive = bytearray(width * height * 3)
                if event_type == "blind_detected":
                    confidence = float(event.get("confidence", 0.0))
                    confidence = (
                        max(0.0, min(1.0, confidence))
                        if math.isfinite(confidence)
                        else 0.0
                    )
                    expected_height = int(event.get("expected_height", height))
                    if not height <= expected_height <= 2048:
                        raise SstvDecodeError("The decoder returned an invalid expected image height")
                    acquisition = {
                        "method": "blind",
                        "confidence": confidence,
                        "recovered_lines": height,
                        "expected_lines": expected_height,
                        "row_origin_known": False,
                        "mode_inferred": bool(event.get("mode_inferred", False)),
                        "color_phase_inferred": bool(event.get("color_phase_inferred", False)),
                        "candidates": [str(value) for value in event.get("candidates", [])],
                    }
                self._publish(
                    {
                        "type": "image_started",
                        "source": "upload",
                        "mode": mode,
                        "width": width,
                        "height": height,
                        "frequency_offset_hz": float(event.get("frequency_offset_hz", 0.0)),
                        "partial": event_type == "blind_detected",
                        "acquisition": acquisition,
                    }
                )
            elif event_type == "line_decoded" and progressive is not None:
                line = int(event["line_index"])
                line_width = int(event["width"])
                rgb = base64.b64decode(str(event["rgb_base64"]), validate=True)
                if line_width != width or len(rgb) != width * 3 or not 0 <= line < height:
                    continue
                start = line * width * 3
                progressive[start : start + len(rgb)] = rgb
                received_lines.add(line)
                self._publish(
                    {
                        "type": "line_decoded",
                        "source": "upload",
                        "line_index": line,
                        "width": width,
                        "rgb_base64": event["rgb_base64"],
                    }
                )
            elif event_type == "image_complete":
                mode = str(event.get("mode") or mode or "SSTV")
                width = int(event["width"])
                height = int(event["height"])
                final_rgb = base64.b64decode(str(event["rgb_base64"]), validate=True)
                if len(final_rgb) != width * height * 3:
                    raise SstvDecodeError("The decoder returned an invalid final image")
                partial = bool(event.get("partial", False))
                if event.get("acquisition") == "blind":
                    recovered = int(event.get("recovered_lines", height))
                    if recovered != height:
                        raise SstvDecodeError("The decoder returned inconsistent partial image metadata")
                    if acquisition is None:
                        confidence = float(event.get("confidence", 0.0))
                        confidence = (
                            max(0.0, min(1.0, confidence))
                            if math.isfinite(confidence)
                            else 0.0
                        )
                        expected_height = int(event.get("expected_height", height))
                        if not height <= expected_height <= 2048:
                            raise SstvDecodeError(
                                "The decoder returned an invalid expected image height"
                            )
                        acquisition = {
                            "method": "blind",
                            "confidence": confidence,
                            "recovered_lines": height,
                            "expected_lines": expected_height,
                            "row_origin_known": False,
                        }

        if final_rgb is None or not mode:
            raise SstvDecodeError("No decodable SSTV image was detected in the uploaded audio")
        diagnostic = self.compare_progressive_to_final(
            progressive, received_lines, final_rgb, width, height
        )
        metadata = self.gallery.save(
            mode=mode,
            width=width,
            height=height,
            rgb=final_rgb,
            partial=partial,
            quality="degraded" if partial else "clean",
            interference_packets=0,
            context={"source": "uploaded"},
            source="uploaded",
            diagnostic=diagnostic,
            acquisition=acquisition,
        )
        with self._lock:
            self._gallery_count = len(self.gallery.list())
        self._publish(
            {
                "type": "image_complete",
                "source": "upload",
                "image": metadata,
                "degraded": partial,
            }
        )
        return metadata

    def set_rx_gain_db(self, value: Any) -> dict[str, Any]:
        """Trim the level of the decoder's own copy of the receive audio.

        This affects only the secondary stream that feeds slowrx. The radio, the
        browser playback level, and every other audio consumer are untouched.
        """
        gain = clamp_gain_db(value)
        with self._lock:
            self._rx_gain_db = gain
        return self.snapshot()

    def apply_rx_gain(self, pcm: bytes) -> bytes:
        """Return a scaled copy of ``pcm`` for the decoder, clipped to range."""
        return apply_gain_db(pcm, self._rx_gain_db)

    def _publish_debug(self, level: str, message: str) -> None:
        self._publish({"type": "debug_log", "level": level, "message": message[:512]})

    def set_enabled(self, enabled: bool) -> dict[str, Any]:
        if type(enabled) is not bool:
            raise ValueError("enabled must be a boolean")
        with self._lifecycle_lock:
            if enabled:
                with self._lock:
                    if self._enabled:
                        return self.snapshot()
                controller = self.get_controller()
                if controller is None:
                    raise ValueError("The radio controller is unavailable; connect the radio before enabling SSTV")
                snapshot_reader = getattr(controller, "try_snapshot", None)
                try:
                    radio = snapshot_reader() if snapshot_reader is not None else controller.snapshot()
                except Exception as exc:
                    raise ValueError("Unable to verify the radio connection; enable SSTV after it reconnects") from exc
                if not radio or not radio.get("connected"):
                    raise ValueError("The radio is disconnected; wait for it to reconnect before enabling SSTV")
                with self._lock:
                    self._enabled = True
                    self._state = self.WAITING
                    self._error = None
                    self._fatal_worker = False
                    self._decoder_peak = 0
                    self._audio_capture.clear()
                    self._audio_capture_bytes = 0
                    self._audio_capture_rate = 0
                    self._stop.clear()
                    self._thread = Thread(target=self._run, name="sstv-decoder", daemon=True)
                    thread = self._thread
                LOGGER.info("SSTV decoder enabled; starting RX audio consumer")
                self._publish_debug("info", "SSTV decoder enabled; starting RX audio consumer.")
                thread.start()
                self._publish_status()
            else:
                with self._lock:
                    if not self._enabled:
                        return self.snapshot()
                    self._enabled = False
                    self._state = self.DISABLED
                    self._error = None
                    self._mode = None
                    self._line = 0
                    self._total_lines = 0
                    self._frequency_offset_hz = None
                    self._current = None
                    self._last_packet_sequence = None
                    self._dropped_packets = 0
                    self._interference_packets = 0
                    thread = self._thread
                    self._thread = None
                self._publish_debug("info", "SSTV decoder disabled by operator.")
                self._stop.set()
                self._wake.set()
                self._terminate_worker()
                if thread is not None:
                    thread.join(timeout=3.0)
                self._publish_status()
        return self.snapshot()

    def shutdown(self) -> None:
        self.set_enabled(False)

    def subscribe(self) -> Queue[dict[str, Any]]:
        subscriber: Queue[dict[str, Any]] = Queue(maxsize=1024)
        with self._lock:
            self._subscribers.append(subscriber)
        return subscriber

    def unsubscribe(self, subscriber: Queue[dict[str, Any]]) -> None:
        with self._lock:
            self._subscribers = [item for item in self._subscribers if item is not subscriber]

    def current_png(self) -> bytes | None:
        with self._lock:
            if self._current is None:
                return None
            width = int(self._current["width"])
            height = int(self._current["height"])
            rgb = bytes(self._current["pixels"])
        return encode_png(width, height, rgb)

    def _capture_decoder_pcm(self, pcm: bytes, sample_rate: int) -> None:
        with self._lock:
            if sample_rate != self._audio_capture_rate:
                self._audio_capture.clear()
                self._audio_capture_bytes = 0
                self._audio_capture_rate = sample_rate
            self._audio_capture.append(pcm)
            self._audio_capture_bytes += len(pcm)
            limit = sample_rate * 2 * _DECODER_CAPTURE_SECONDS
            while self._audio_capture_bytes > limit:
                excess = self._audio_capture_bytes - limit
                first = self._audio_capture.popleft()
                removed = min(excess, len(first))
                self._audio_capture_bytes -= removed
                if removed < len(first):
                    self._audio_capture.appendleft(first[removed:])

    def decoder_audio_wav(self) -> bytes | None:
        with self._lock:
            chunks = tuple(self._audio_capture)
            sample_rate = self._audio_capture_rate
        if not chunks or not sample_rate:
            return None
        output = io.BytesIO()
        with wave.open(output, 'wb') as recording:
            recording.setnchannels(1)
            recording.setsampwidth(2)
            recording.setframerate(sample_rate)
            for chunk in chunks:
                recording.writeframesraw(chunk)
        return output.getvalue()

    @staticmethod
    def extract_rx_pcm(packet: bytes, channels: int) -> bytes:
        if channels not in (1, 2) or len(packet) < 24:
            return b""
        pcm_length = int.from_bytes(packet[22:24], "big")
        pcm = packet[24:]
        if not pcm_length or pcm_length != len(pcm) or len(pcm) % (channels * 2):
            return b""
        if channels == 1:
            return pcm
        samples = array("h")
        samples.frombytes(pcm)
        if sys.byteorder != "little":
            samples.byteswap()
        # Native Pi-Sat maps MAIN to left and tracked SUB/RX to right.
        sub_samples = samples[1::2]
        if sys.byteorder != "little":
            sub_samples.byteswap()
        return sub_samples.tobytes()

    @staticmethod
    def _pcm_rms_dbfs(pcm: bytes) -> float | None:
        usable = len(pcm) - (len(pcm) % 2)
        if not usable:
            return None
        samples = array("h")
        samples.frombytes(pcm[:usable])
        if sys.byteorder != "little":
            samples.byteswap()
        rms = math.sqrt(sum(int(sample) * int(sample) for sample in samples) / len(samples))
        return 20.0 * math.log10(rms / 32768.0) if rms > 0 else None

    @staticmethod
    def _packet_sequence(packet: bytes) -> int | None:
        if len(packet) < _AUDIO_SEQUENCE_OFFSET + 2:
            return None
        return int.from_bytes(packet[_AUDIO_SEQUENCE_OFFSET : _AUDIO_SEQUENCE_OFFSET + 2], "big")

    def _packet_gap(self, packet: bytes) -> int:
        current = self._packet_sequence(packet)
        if current is None:
            return 0
        previous = self._last_packet_sequence
        self._last_packet_sequence = current
        if previous is None:
            return 0
        delta = (current - previous) & 0xFFFF
        if delta == 0 or delta > _MAX_REASONABLE_GAP_PACKETS:
            return 0
        return delta - 1

    def _handle_audio_gap(self, missing_packets: int) -> None:
        with self._lock:
            self._dropped_packets += missing_packets
            self._interference_packets += missing_packets
            current = self._current
            if current is not None:
                current["quality"] = "degraded"
                current["interference_packets"] = int(current.get("interference_packets", 0)) + missing_packets
            decoding = self._state in (self.DETECTED, self.DECODING)
        major = missing_packets > _MINOR_GAP_PACKETS
        LOGGER.warning(
            "SSTV audio gap detected missing_packets=%d action=%s",
            missing_packets,
            "restart" if major else "continue",
        )
        self._publish(
            {
                "type": "audio_gap",
                "missing_packets": missing_packets,
                "severity": "major" if major else "interference",
            }
        )
        if decoding:
            self._publish_status()
        if major:
            self._save_current_as_degraded()

    def _save_current_as_degraded(self) -> None:
        with self._lock:
            current = self._current
            if (
                current is None
                or int(current.get("last_line", 0)) <= 0
                or current.get("partial_saved")
            ):
                return
            mode = str(current["mode"])
            width = int(current["width"])
            height = int(current["height"])
            rgb = bytes(current["pixels"])
            context = dict(self._capture_context or self.get_context())
            interference_packets = int(current.get("interference_packets", 0))
            current["quality"] = "degraded"
        metadata = self.gallery.save(
            mode=mode,
            width=width,
            height=height,
            rgb=rgb,
            partial=True,
            quality="degraded",
            interference_packets=interference_packets,
            context=context,
        )
        with self._lock:
            if self._current is current:
                current["partial_saved"] = True
            self._gallery_count = len(self.gallery.list())
        self._publish({"type": "image_complete", "image": metadata, "degraded": True})

    def _run(self) -> None:
        LOGGER.info("SSTV RX audio consumer thread started")
        controller = None
        audio_queue = None
        sample_rate = None
        channels = 0
        connected = False
        reported_connection: bool | None = None
        debug_window_started = monotonic()
        debug_packets = 0
        debug_samples = 0
        debug_queue_drops = 0
        last_input_rms_dbfs: float | None = None
        try:
            while not self._stop.is_set():
                next_controller = self.get_controller()
                if next_controller is not controller:
                    if controller is not None and audio_queue is not None:
                        controller.unsubscribe_audio(audio_queue)
                    self._terminate_worker()
                    controller = next_controller
                    audio_queue = None
                    sample_rate = None
                    channels = 0
                    connected = False
                    with self._lock:
                        self._last_audio_at = None
                        self._last_packet_sequence = None
                    if controller is not None:
                        sample_rate = int(controller.config.sample_rate)
                        channels = 2 if controller.config.rx_codec == "lpcm16_stereo" else 1
                        audio_queue = controller.subscribe_audio(
                            self._wake.set,
                            max_seconds=_SSTV_AUDIO_BUFFER_SECONDS,
                        )
                        channel_text = "stereo SUB/RX channel" if channels == 2 else "mono RX"
                        LOGGER.info(
                            "SSTV subscribed to radio RX audio: %d Hz %s",
                            sample_rate,
                            channel_text,
                        )
                        self._publish_debug(
                            "info",
                            f"Subscribed to radio RX audio: {sample_rate} Hz {channel_text}.",
                        )
                    else:
                        LOGGER.warning("SSTV radio audio controller unavailable")
                        self._publish_debug("warn", "Radio audio controller unavailable; waiting for it to appear.")
                    reported_connection = None

                if controller is not None:
                    try:
                        snapshot_reader = getattr(controller, "try_snapshot", None)
                        radio = snapshot_reader() if snapshot_reader is not None else controller.snapshot()
                        if radio is not None:
                            connected = bool(radio.get("connected"))
                    except Exception:
                        pass
                if connected != reported_connection:
                    LOGGER.info(
                        "SSTV radio connection %s",
                        "up" if connected else "down",
                    )
                    self._publish_debug(
                        "info" if connected else "warn",
                        "Radio connection is up." if connected else "Radio is disconnected; RX audio is unavailable.",
                    )
                    reported_connection = connected
                packets = controller.read_audio(audio_queue) if connected and audio_queue is not None else []
                if packets:
                    debug_packets += len(packets)
                    if not self._ensure_worker(int(sample_rate)):
                        self._wake.wait(0.5)
                        self._wake.clear()
                        continue
                    wrote_samples = 0
                    for packet in packets:
                        gap = self._packet_gap(packet)
                        if gap:
                            self._handle_audio_gap(gap)
                            if gap > _MINOR_GAP_PACKETS:
                                # A large missing span destroys timing context. Do
                                # not synthesize samples or let slowrx bridge it;
                                # terminate the decoder so the next packet starts
                                # a fresh VIS search boundary.
                                self._terminate_worker()
                                if not self._ensure_worker(int(sample_rate)):
                                    continue
                        pcm = self.extract_rx_pcm(packet, channels)
                        if not pcm:
                            continue
                        input_level_dbfs = self._pcm_rms_dbfs(pcm)
                        last_input_rms_dbfs = input_level_dbfs
                        stream = apply_gain_db(pcm, self._rx_gain_db)
                        with self._lock:
                            self._decoder_peak = max(peak_of(stream), int(self._decoder_peak * 0.9))
                        if self._write_pcm(stream):
                            self._capture_decoder_pcm(stream, int(sample_rate))
                            wrote_samples += len(stream) // 2
                    if wrote_samples:
                        debug_samples += wrote_samples
                        with self._lock:
                            self._input_samples += wrote_samples
                            self._last_audio_at = monotonic()
                            resume_listening = (
                                self._state in (self.WAITING, self.COMPLETE, self.FAILED)
                                and not self._fatal_worker
                                and monotonic() >= self._terminal_until
                            )
                        if resume_listening:
                            self._set_state(self.LISTENING)
                else:
                    with self._lock:
                        last_audio_at = self._last_audio_at
                        decoding = self._state in (self.DETECTED, self.DECODING)
                    if not connected or last_audio_at is None or monotonic() - last_audio_at > 2.0:
                        if not decoding and not self._fatal_worker:
                            self._set_state(self.WAITING)
                    self._wake.wait(0.25)
                    self._wake.clear()
                # slowrx buffers a full mode-duration before emitting pixels.
                # Audio amplitude cannot identify completion or justify a reset.
                debug_now = monotonic()
                debug_elapsed = debug_now - debug_window_started
                if debug_elapsed >= 5.0:
                    queue_drops = int(getattr(audio_queue, "dropped", 0))
                    new_queue_drops = max(0, queue_drops - debug_queue_drops)
                    if debug_packets:
                        status = self.snapshot()
                        input_rms_text = (
                            f"{last_input_rms_dbfs:.1f}"
                            if last_input_rms_dbfs is not None
                            else "silent"
                        )
                        input_path = "stereo to SUB/RX mono" if channels == 2 else "mono RX"
                        LOGGER.info(
                            "SSTV RX feed: %d packets, %d samples in %.1fs; "
                            "%d Hz %s; decoder peak=%s dBFS; latest packet RMS=%s dBFS; "
                            "sequence gaps=%d; local queue drops=%d",
                            debug_packets,
                            debug_samples,
                            debug_elapsed,
                            sample_rate,
                            input_path,
                            status["decoder_level_dbfs"],
                            input_rms_text,
                            status["dropped_packets"],
                            queue_drops,
                        )
                        self._publish_debug(
                            "warn" if new_queue_drops else "info",
                            f"RX feed: {debug_packets} packets, {debug_samples} samples in {debug_elapsed:.1f}s; "
                            f"{sample_rate} Hz {input_path}; decoder peak {status['decoder_level_dbfs']} dBFS; "
                            f"latest packet RMS {input_rms_text} dBFS; "
                            f"sequence gaps total {status['dropped_packets']}; local queue drops "
                            f"{new_queue_drops} this interval/{queue_drops} total.",
                        )
                    else:
                        LOGGER.warning(
                            "SSTV received no RX audio packets in %.1fs (radio connected=%s, state=%s, local queue drops=%d)",
                            debug_elapsed,
                            connected,
                            self.snapshot()["state"],
                            queue_drops,
                        )
                        self._publish_debug(
                            "warn" if new_queue_drops or connected else "info",
                            f"No RX audio packets in {debug_elapsed:.1f}s (radio connected: {connected}; "
                            f"decoder state: {self.snapshot()['state']}; local queue drops "
                            f"{new_queue_drops} this interval/{queue_drops} total).",
                        )
                    debug_window_started = debug_now
                    debug_packets = 0
                    debug_samples = 0
                    debug_queue_drops = queue_drops
        except Exception as exc:
            LOGGER.exception("SSTV audio consumer failed")
            self._set_failure(str(exc), fatal=True)
        finally:
            if controller is not None and audio_queue is not None:
                controller.unsubscribe_audio(audio_queue)
            self._terminate_worker()

    def _decoder_path(self) -> Path | None:
        override = os.environ.get("PI_SAT_SSTV_DECODER")
        candidates = []
        if override:
            candidates.append(Path(override).expanduser())
        suffix = ".exe" if os.name == "nt" else ""
        candidates.extend(
            [
                self.project_root / "bin" / f"pi-sat-sstv-decoder{suffix}",
            ]
        )
        path_entry = shutil.which("pi-sat-sstv-decoder")
        if path_entry:
            candidates.append(Path(path_entry))
        return next((path for path in candidates if path.is_file()), None)

    def _ensure_worker(self, sample_rate: int) -> bool:
        with self._process_lock:
            if self._process is not None and self._process.poll() is None:
                return True
            path = self._decoder_path()
            if path is None:
                self._set_failure(
                    "bundled slowrx worker is unavailable; install the 64-bit Pi-Sat package with bin/pi-sat-sstv-decoder",
                    fatal=True,
                )
                return False
            try:
                process = subprocess.Popen(
                    [str(path), str(sample_rate)],
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    bufsize=0,
                    shell=False,
                )
            except OSError as exc:
                self._set_failure(f"Unable to start slowrx worker: {exc}", fatal=True)
                return False
            self._process = process
            self._process_generation += 1
            generation = self._process_generation
            self._last_worker_error = None
        LOGGER.info("SSTV slowrx worker started: %s (%d Hz)", path.name, sample_rate)
        self._publish_debug("info", f"slowrx worker started: {path.name}, input rate {sample_rate} Hz.")
        Thread(
            target=self._read_worker_events,
            args=(process, generation),
            name="sstv-events",
            daemon=True,
        ).start()
        Thread(
            target=self._read_worker_errors,
            args=(process, generation),
            name="sstv-errors",
            daemon=True,
        ).start()
        with self._lock:
            self._fatal_worker = False
        return True

    def _write_pcm(self, pcm: bytes) -> bool:
        with self._process_lock:
            process = self._process
            if process is None or process.stdin is None or process.poll() is not None:
                return False
        # The event reader must drain stdout while stdin is blocked. Holding
        # the lifecycle lock here deadlocks when both pipe buffers are full.
        try:
            remaining = memoryview(pcm)
            while remaining:
                written = process.stdin.write(remaining)
                if not written:
                    raise BrokenPipeError("decoder input accepted no bytes")
                remaining = remaining[written:]
            process.stdin.flush()
            return True
        except (OSError, ValueError) as exc:
            with self._process_lock:
                active = process is self._process
            if active and not self._stop.is_set():
                self._set_failure(f"slowrx worker input failed: {exc}", fatal=True)
            return False

    def _terminate_worker(self) -> None:
        with self._process_lock:
            process = self._process
            self._process = None
            self._process_generation += 1
        if process is None:
            return
        # Stop the reader first so a blocked stdin write can return before
        # closing its file object (close may itself wait for a pending write).
        if process.poll() is None:
            try:
                process.terminate()
            except ProcessLookupError:
                pass
            try:
                process.wait(timeout=1.0)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=1.0)
        if process.stdin is not None:
            try:
                process.stdin.close()
            except OSError:
                pass

    def _read_worker_events(self, process: subprocess.Popen[bytes], generation: int) -> None:
        if process.stdout is None:
            return
        try:
            for raw_line in iter(process.stdout.readline, b""):
                with self._process_lock:
                    if generation != self._process_generation or process is not self._process:
                        return
                try:
                    event = json.loads(raw_line)
                    if not isinstance(event, dict):
                        raise ValueError("worker event is not an object")
                    self._handle_worker_event(event)
                except Exception as exc:
                    LOGGER.warning("Ignored invalid slowrx worker event: %s", exc)
                    self._set_failure(f"slowrx event handling failed: {exc}", fatal=False)
        finally:
            process.stdout.close()
            with self._process_lock:
                active = generation == self._process_generation and process is self._process
            if active and not self._stop.is_set():
                exit_code = process.poll()
                with self._lock:
                    worker_error = self._last_worker_error
                message = f"slowrx worker exited unexpectedly (code {exit_code})"
                if worker_error:
                    message = f"{message}: {worker_error}"
                self._set_failure(message, fatal=True)

    def _read_worker_errors(self, process: subprocess.Popen[bytes], generation: int) -> None:
        if process.stderr is None:
            return
        try:
            for raw_line in iter(process.stderr.readline, b""):
                with self._process_lock:
                    if generation != self._process_generation or process is not self._process:
                        return
                message = raw_line.decode("utf-8", errors="replace").strip()
                if message:
                    with self._lock:
                        self._last_worker_error = message[-512:]
                    LOGGER.warning("slowrx: %s", message)
                    self._publish_debug("warn", f"slowrx stderr: {message}")
        finally:
            process.stderr.close()

    def _handle_worker_event(self, event: dict[str, Any]) -> None:
        with self._lock:
            if not self._enabled:
                return
        event_type = event.get("type")
        if event_type == "ready":
            LOGGER.info("SSTV slowrx worker ready; scanning for VIS headers")
            self._publish_debug("info", "slowrx worker ready; scanning audio for SSTV VIS headers.")
            return
        if event_type == "vis_detected":
            width = int(event["width"])
            height = int(event["height"])
            if not 0 < width <= 2048 or not 0 < height <= 2048:
                raise ValueError("slowrx returned invalid image dimensions")
            with self._lock:
                self._mode = str(event["mode"])
                self._line = 0
                self._total_lines = height
                self._frequency_offset_hz = float(event["frequency_offset_hz"])
                self._capture_context = dict(self.get_context())
                self._current = {
                    "mode": self._mode,
                    "width": width,
                    "height": height,
                    "last_line": 0,
                    "quality": "clean",
                    "interference_packets": 0,
                    "pixels": bytearray(width * height * 3),
                    "received_lines": set(),
                }
            LOGGER.info(
                "SSTV VIS detected: %s %dx%d at %+.1f Hz",
                self._mode,
                width,
                height,
                self._frequency_offset_hz,
            )
            self._set_state(self.DETECTED)
            self._publish_debug(
                "info",
                f"VIS detected: {self._mode}, {width}x{height}, "
                f"frequency offset {self._frequency_offset_hz:+.1f} Hz.",
            )
            self._publish(
                {
                    "type": "image_started",
                    "mode": self._mode,
                    "width": width,
                    "height": height,
                    "frequency_offset_hz": self._frequency_offset_hz,
                }
            )
            return
        if event_type == "unknown_vis":
            code = event.get("code")
            LOGGER.warning("SSTV unknown VIS code: %s", code)
            self._publish_debug("warn", f"Unknown SSTV VIS code {code}.")
            self._set_failure(f"Unknown SSTV VIS code {code}", fatal=False)
            return
        if event_type == "line_decoded":
            rgb = base64.b64decode(str(event["rgb_base64"]), validate=True)
            line_index = int(event["line_index"])
            width = int(event["width"])
            if len(rgb) != width * 3:
                raise ValueError("slowrx line buffer length is invalid")
            drop_reason = None
            line_height = 0
            with self._lock:
                current = self._current
                if current is None:
                    drop_reason = "no active image"
                elif width != current["width"]:
                    drop_reason = f"worker width {width} != image width {current['width']}"
                    line_height = int(current["height"])
                elif not 0 <= line_index < current["height"]:
                    drop_reason = f"line index {line_index} outside image height {current['height']}"
                    line_height = int(current["height"])
                else:
                    line_height = int(current["height"])
                    start = line_index * width * 3
                    current["pixels"][start : start + len(rgb)] = rgb
                    current["last_line"] = max(current["last_line"], line_index + 1)
                    current["received_lines"].add(line_index)
                    self._line = current["last_line"]
            if drop_reason:
                LOGGER.warning(
                    "SSTV slowrx line %d discarded by backend: %s",
                    line_index + 1,
                    drop_reason,
                )
                self._publish_debug(
                    "error",
                    f"slowrx emitted line {line_index + 1}, but Pi-Sat discarded it: {drop_reason}.",
                )
                return
            LOGGER.debug("SSTV slowrx line %d/%d accepted", line_index + 1, line_height)
            self._publish_debug(
                "info",
                f"slowrx emitted line {line_index + 1}/{line_height}; Pi-Sat accepted and forwarding it to the page.",
            )
            self._set_state(self.DECODING)
            self._publish(
                {
                    "type": "line_decoded",
                    "line_index": line_index,
                    "width": width,
                    "rgb_base64": event["rgb_base64"],
                }
            )
            return
        if event_type == "image_complete":
            width = int(event["width"])
            height = int(event["height"])
            rgb = base64.b64decode(str(event["rgb_base64"]), validate=True)
            if len(rgb) != width * height * 3:
                raise ValueError("slowrx image buffer length is invalid")
            with self._lock:
                mode = str(event["mode"])
                previous_quality = str(self._current.get("quality", "clean")) if self._current else "clean"
                previous_interference = int(self._current.get("interference_packets", 0)) if self._current else 0
                progressive = bytes(self._current.get("pixels", b"")) if self._current else None
                received_lines = set(self._current.get("received_lines", set())) if self._current else set()
                self._mode = mode
                self._line = height
                self._total_lines = height
                self._current = {
                    "mode": mode,
                    "width": width,
                    "height": height,
                    "last_line": height,
                    "quality": "degraded" if event.get("partial", False) or previous_quality != "clean" else "clean",
                    "interference_packets": previous_interference,
                    "pixels": bytearray(rgb),
                    "received_lines": set(range(height)),
                }
                context = dict(self._capture_context or self.get_context())
                quality = str(self._current["quality"])
                interference_packets = int(self._current["interference_packets"])
            diagnostic = self.compare_progressive_to_final(
                bytearray(progressive) if progressive is not None else None,
                received_lines,
                rgb,
                width,
                height,
            )
            metadata = self.gallery.save(
                mode=mode,
                width=width,
                height=height,
                rgb=rgb,
                partial=bool(event.get("partial", False)),
                quality=quality,
                interference_packets=interference_packets,
                context=context,
                source="radio",
                diagnostic=diagnostic,
            )
            with self._lock:
                self._gallery_count = len(self.gallery.list())
                self._terminal_until = monotonic() + 3.0
            LOGGER.info(
                "SSTV image complete: %s %dx%d, quality=%s, missing_packets=%d",
                mode,
                width,
                height,
                quality,
                interference_packets,
            )
            self._publish_debug(
                "info" if quality == "clean" else "warn",
                f"Image complete: {mode}, {width}x{height}, quality {quality}, "
                f"{interference_packets} missing audio packets; saved {metadata.get('filename', 'image')}.",
            )
            self._set_state(self.COMPLETE)
            self._publish({"type": "image_complete", "source": "radio", "image": metadata})

    def _set_failure(self, message: str, *, fatal: bool) -> None:
        with self._lock:
            if not self._enabled:
                return
            if self._state == self.FAILED and self._error == message and self._fatal_worker == fatal:
                return
            self._state = self.FAILED
            self._error = message
            self._fatal_worker = fatal
            self._terminal_until = float("inf") if fatal else monotonic() + 3.0
        self._publish_debug("error", message)
        self._publish_status()

    def _set_state(self, state: str) -> None:
        with self._lock:
            if not self._enabled or (self._state == state and self._error is None):
                return
            self._state = state
            self._error = None
        self._publish_status()

    def _publish_status(self) -> None:
        self._publish({"type": "status", "status": self.snapshot()})

    def _publish(self, event: dict[str, Any]) -> None:
        with self._lock:
            self._sequence += 1
            event = {**event, "sequence": self._sequence}
            subscribers = list(self._subscribers)
        for subscriber in subscribers:
            try:
                subscriber.put_nowait(event)
            except Full:
                try:
                    subscriber.get_nowait()
                except Empty:
                    pass
                try:
                    subscriber.put_nowait(event)
                except Full:
                    pass
