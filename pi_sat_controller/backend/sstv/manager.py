from __future__ import annotations

from array import array
from collections import deque
import base64
from collections.abc import Callable
import json
import logging
import math
import os
from pathlib import Path
from queue import Empty, Full, Queue
import shutil
import subprocess
import sys
from threading import Event, Lock, RLock, Thread
from time import monotonic
from typing import Any

from pi_sat_controller.backend.audio_gain import apply_gain_db, clamp_gain_db, dbfs_of, peak_of
from pi_sat_controller.backend.sstv.gallery import SstvGallery, encode_png
from pi_sat_controller.backend.sstv.events import SSTV_MODE_DIMENSIONS, SstvEventAdapter
from pi_sat_controller.backend.sstv.worker import MAX_FRAME_BYTES, frame_control, frame_pcm
from pi_sat_controller.backend.sstv.capture import SstvAudioCapture


LOGGER = logging.getLogger(__name__)

_AUDIO_SEQUENCE_OFFSET = 18
_MINOR_GAP_PACKETS = 3
_MAX_REASONABLE_GAP_PACKETS = 0x7FFF
_FILE_SAMPLE_RATE = 16_000
_FILE_DECODE_TIMEOUT_S = 180.0
_MAX_FILE_PCM_BYTES = 128 * 1024 * 1024
_WATCHDOG_INTERVAL_S = 1.0
_WORKER_STALL_S = 30.0
_RETRY_MAX_S = 30.0


class SstvDecodeError(RuntimeError):
    """Raised when uploaded audio cannot produce a complete or partial image."""


class SstvManager:
    """Owns one SSTVFlow worker fed from a copy of native Icom SUB/RX PCM."""

    DISABLED = "Disabled"
    WAITING = "Waiting for radio audio"
    LISTENING = "Listening"
    DETECTED = "SSTV detected"
    DECODING = "Decoding"
    COMPLETE = "Decode complete"
    FAILED = "Decode failed"
    FINISHING = "Finishing queued audio"
    RECOVERING = "Recovering decoder"

    def __init__(
        self,
        *,
        project_root: Path,
        data_dir: Path,
        get_controller: Callable[[], Any],
        get_context: Callable[[], dict[str, Any]],
        rx_gain_db: float = -6.0,
        on_image_saved: Callable[[Path, dict[str, Any]], None] | None = None,
    ) -> None:
        self.project_root = Path(project_root)
        self.gallery = SstvGallery(data_dir, retention=25)
        self.get_controller = get_controller
        self.get_context = get_context
        self.on_image_saved = on_image_saved
        self._lock = RLock()
        self._lifecycle_lock = Lock()
        self._process_lock = Lock()
        self._write_lock = Lock()
        self._termination_lock = Lock()
        self._closing_process: subprocess.Popen[bytes] | None = None
        self._reader_done = Event()
        self._enabled = False
        self._state = self.DISABLED
        self._error: str | None = None
        self._mode: str | None = None
        self._line = 0
        self._total_lines = 0
        self._frequency_offset_hz: float | None = None
        # Decoder-reported bounded receive window while it holds through a fade.
        self._holding_signal = False
        self._receive_seconds_remaining: float | None = None
        self._input_samples = 0
        self._audio_capture = SstvAudioCapture(Path(data_dir))
        self._audio_queue = None
        self._audio_controller = None
        self._input_sample_rate = 0
        self._pending_batch_samples = 0
        self._finish_requested = False
        self._input_level_dbfs: float | None = None
        self._clipped_samples = 0
        self._radio_audio_diagnostics: dict[str, Any] = {}
        self._reported_capture_error: str | None = None
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
        self._consumer_thread: Thread | None = None
        self._recovery_requested = Event()
        self._supervisor_wake = Event()
        self._pending_packets: deque[bytes] = deque()
        self._health = "disabled"
        self._restart_count = 0
        self._recovery_reason: str | None = None
        self._worker_started_at: float | None = None
        self._worker_closing_at: float | None = None
        self._worker_ready = False
        self._worker_input_samples = 0
        self._worker_sample_position = 0
        self._worker_progress_at: float | None = None
        self._worker_pending_since: float | None = None
        self._write_started_at: float | None = None
        self._consumer_progress_at: float | None = None
        self._last_packet_at: float | None = None
        self._radio_audio_at: float | None = None
        self._radio_audio_count: int | None = None
        self._radio_connected = False
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
                    for key in ("mode", "width", "height", "last_line", "image_id", "acquisition")
                }
            return {
                "enabled": self._enabled,
                "state": (self.FINISHING if self._finish_requested else
                          self.RECOVERING if self._health == "recovering" else
                          self.WAITING if self._enabled and self._health in {"starting", "waiting"}
                          else self._state),
                "health": self._health,
                "restart_count": self._restart_count,
                "recovery_reason": self._recovery_reason,
                "processed_samples": self._worker_sample_position,
                "last_processed_age_seconds": (round(monotonic() - self._worker_progress_at, 1)
                                               if self._worker_progress_at is not None else None),
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
                "input_level_dbfs": self._input_level_dbfs,
                "clipped_samples": self._clipped_samples,
                "queued_audio_seconds": round(
                    float(getattr(self._audio_queue, "pending_seconds", 0.0))
                    + self._pending_batch_samples / max(1, self._input_sample_rate), 3),
                "capture_error": self._audio_capture.status()["error"],
                "radio_audio_diagnostics": dict(self._radio_audio_diagnostics),
                "image_quality": self._current.get("quality") if self._current else None,
                "holding_signal": self._holding_signal,
                "receive_seconds_remaining": self._receive_seconds_remaining,
                "interference_packets": self._interference_packets,
                "dropped_packets": self._dropped_packets,
                "gallery_count": self._gallery_count,
                "audio_source": "Native IC-9700 SUB/RX PCM",
                "decoder": "SSTVFlow 0.2.0",
                "supported_modes": list(SSTV_MODE_DIMENSIONS),
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
        """Describe changes between rendered lines and the authoritative final image."""
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
                self._decoder_command(_FILE_SAMPLE_RATE),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                bufsize=0,
                shell=False,
            )
            framed = b"".join(frame_pcm(pcm[start:start + MAX_FRAME_BYTES])
                              for start in range(0, len(pcm), MAX_FRAME_BYTES))
            stdout, _ = process.communicate(input=framed, timeout=_FILE_DECODE_TIMEOUT_S)
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
        final_event: dict[str, Any] = {}
        final_progressive: bytearray | None = None
        final_received_lines: set[int] = set()
        adapter = SstvEventAdapter()
        for raw_line in stdout.splitlines():
            try:
                event = json.loads(raw_line)
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise SstvDecodeError("The SSTV decoder returned invalid event data") from exc
            if not isinstance(event, dict):
                continue
            event = adapter.adapt(event)
            if event is None:
                continue
            event_type = event.get("type")
            if event_type == "vis_detected":
                received_lines.clear()
                acquisition = event.get("acquisition")
                mode = str(event.get("mode") or "SSTV")
                width = int(event["width"])
                height = int(event["height"])
                if not 0 < width <= 2048 or not 0 < height <= 2048:
                    raise SstvDecodeError("The decoder returned invalid image dimensions")
                progressive = bytearray(width * height * 3)
                self._publish(
                    {
                        "type": "image_started",
                        "source": "upload",
                        "image_id": event.get("image_id"),
                        "mode": mode,
                        "width": width,
                        "height": height,
                        "frequency_offset_hz": float(event.get("frequency_offset_hz", 0.0)),
                        "partial": bool(event.get("partial", False)),
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
                        "height": height,
                        "mode": mode,
                        "row_revision": event.get("row_revision", False),
                        "revision": event.get("revision", 0),
                        "image_id": event.get("image_id"),
                    }
                )
            elif event_type == "image_complete":
                final_event = event
                final_progressive = bytearray(progressive) if progressive is not None else None
                final_received_lines = set(received_lines)
                acquisition = event.get("acquisition")
                mode = str(event.get("mode") or mode or "SSTV")
                width = int(event["width"])
                height = int(event["height"])
                final_rgb = base64.b64decode(str(event["rgb_base64"]), validate=True)
                if len(final_rgb) != width * height * 3:
                    raise SstvDecodeError("The decoder returned an invalid final image")
                partial = bool(event.get("partial", False))

        if final_rgb is None or not final_event:
            raise SstvDecodeError("No decodable SSTV image was detected in the uploaded audio")
        mode = str(final_event["mode"])
        width, height = int(final_event["width"]), int(final_event["height"])
        partial = bool(final_event.get("partial"))
        acquisition = final_event.get("acquisition")
        diagnostic = self.compare_progressive_to_final(
            final_progressive, final_received_lines, final_rgb, width, height
        )
        metadata = self.gallery.save(
            mode=mode,
            width=width,
            height=height,
            rgb=final_rgb,
            partial=partial,
            quality=str(final_event.get("quality", "degraded" if partial else "clean")),
            interference_packets=0,
            context={"source": "uploaded"},
            source="uploaded",
            diagnostic=diagnostic,
            acquisition=acquisition,
            png=final_event.get("png_bytes"),
            reception=final_event.get("reception"),
        )
        self._notify_image_saved(metadata)
        with self._lock:
            self._gallery_count = len(self.gallery.list())
        self._publish(
            {
                "type": "image_complete",
                "source": "upload",
                "image": metadata,
                "degraded": final_event.get("quality") == "degraded",
            }
        )
        return metadata

    def set_rx_gain_db(self, value: Any) -> dict[str, Any]:
        """Trim the level of the decoder's own copy of the receive audio.

        This affects only the secondary stream that feeds SSTVFlow. The radio, the
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
                try:
                    self._audio_capture.start()
                except OSError as exc:
                    raise ValueError(f"Unable to start SSTV audio capture: {exc}") from exc
                with self._lock:
                    self._enabled = True
                    self._state = self.WAITING
                    self._error = None
                    self._fatal_worker = False
                    self._decoder_peak = 0
                    self._finish_requested = False
                    self._input_samples = 0
                    self._input_level_dbfs = None
                    self._clipped_samples = 0
                    self._radio_audio_diagnostics = {}
                    self._reported_capture_error = None
                    self._pending_batch_samples = 0
                    self._last_packet_sequence = None
                    self._dropped_packets = 0
                    self._interference_packets = 0
                    self._pending_packets.clear()
                    self._health = "starting"
                    self._restart_count = 0
                    self._recovery_reason = None
                    self._last_audio_at = None
                    self._last_packet_at = None
                    self._radio_audio_at = None
                    self._radio_audio_count = None
                    self._radio_connected = True
                    self._recovery_requested.clear()
                    self._supervisor_wake.clear()
                    self._stop.clear()
                    self._thread = Thread(target=self._supervise, name="sstv-watchdog", daemon=True)
                    thread = self._thread
                LOGGER.info("SSTV decoder enabled; starting RX audio consumer")
                self._publish_debug("info", "SSTV decoder enabled; starting RX audio consumer.")
                thread.start()
                self._publish_status()
            else:
                with self._lock:
                    if not self._enabled:
                        return self.snapshot()
                    self._finish_requested = True
                    thread = self._thread
                    controller = self._audio_controller
                    audio_queue = self._audio_queue
                # Detach reception before draining so a slow decoder has a
                # finite set of accepted samples to finish.
                if controller is not None and audio_queue is not None:
                    controller.unsubscribe_audio(audio_queue)
                self._publish_debug("info", "Reception stopped; finishing queued SSTV audio.")
                self._stop.set()
                self._wake.set()
                self._supervisor_wake.set()
                self._publish_status()
                if thread is not None:
                    thread.join()
                else:
                    self._terminate_worker(drain=True)
                    self._audio_capture.close()
                with self._lock:
                    self._enabled = False
                    self._finish_requested = False
                    self._state = self.DISABLED
                    self._health = "disabled"
                    self._thread = None
                    self._mode = None
                    self._line = 0
                    self._total_lines = 0
                    self._frequency_offset_hz = None
                    self._current = None
                self._publish_status()
        return self.snapshot()

    def shutdown(self) -> None:
        self.set_enabled(False)

    def _watchdog_reason(self, now: float) -> str | None:
        with self._lock:
            if self._fatal_worker:
                return self._error or "SSTV consumer failed"
            started = self._worker_started_at
            progress = self._worker_progress_at
            closing_at = self._worker_closing_at
            latest = max((value for value in (self._consumer_progress_at, progress,
                                              self._last_audio_at, started, closing_at)
                          if value is not None), default=now)
            if started is None:
                return "SSTV audio consumer stopped advancing" if now - latest >= _WORKER_STALL_S else None
            if not self._worker_ready and now - started >= _WORKER_STALL_S:
                return "SSTVFlow worker did not become ready"
            last_progress = progress if progress is not None else started
            if (self._write_started_at is not None
                    and now - max(self._write_started_at, last_progress) >= _WORKER_STALL_S):
                return "SSTVFlow audio input is blocked"
            if (self._worker_pending_since is not None
                    and now - max(self._worker_pending_since, last_progress) >= _WORKER_STALL_S):
                return "SSTVFlow stopped processing queued audio"
            if now - latest >= _WORKER_STALL_S:
                return "SSTV audio consumer stopped advancing"
            if (self._radio_connected and self._radio_audio_at is not None
                    and now - self._radio_audio_at < 2.0
                    and now - (self._last_packet_at if self._last_packet_at is not None else started)
                    >= _WORKER_STALL_S):
                return "SSTV subscription stopped receiving the radio audio stream"
        with self._process_lock:
            closing = self._closing_process is not None
            if self._process is not None and self._process.poll() is not None and not closing:
                return "SSTVFlow worker exited unexpectedly"
        if closing and now - max(last_progress, closing_at or last_progress) >= _WORKER_STALL_S:
            return "SSTVFlow stopped while finishing queued audio"
        return None

    def _interrupt_worker(self, previous: tuple[Any, float] | None) -> tuple[Any, float] | None:
        # Do not wait for the writer or termination lock: either can be held
        # while a stalled process is blocking a pipe write or an EOF drain.
        with self._process_lock:
            process = self._process
            if process is None or process.poll() is not None:
                return None
            self._closing_process = process
        now = monotonic()
        try:
            if previous is None or previous[0] is not process:
                process.terminate()
                return process, now
            if now - previous[1] >= 1.0:
                process.kill()
        except ProcessLookupError:
            return None
        return previous

    def _supervise(self) -> None:
        consumer: Thread | None = None
        retry_at = 0.0
        retry_delay = 1.0
        retrying = False
        healthy_since: float | None = None
        interrupted: tuple[Any, float] | None = None
        try:
            while True:
                now = monotonic()
                if consumer is not None and not consumer.is_alive():
                    consumer.join()
                    consumer = None
                    if not self._stop.is_set() and not self._recovery_requested.is_set():
                        with self._lock:
                            self._recovery_reason = self._error or "SSTV audio consumer exited unexpectedly"
                            self._health = "recovering"
                        self._recovery_requested.set()
                        retry_at = now + retry_delay
                        retry_delay = min(_RETRY_MAX_S, retry_delay * 2)
                        self._publish_debug("error", self._recovery_reason)
                        self._publish_status()
                    retrying = True
                if consumer is None:
                    if self._stop.is_set():
                        break
                    if now >= retry_at:
                        with self._lock:
                            self._fatal_worker = False
                            self._error = None
                            self._state = self.WAITING
                            self._terminal_until = 0.0
                            self._consumer_progress_at = now
                            if retrying:
                                self._restart_count += 1
                                self._current = None
                                self._mode = None
                                self._line = self._total_lines = 0
                                self._frequency_offset_hz = None
                                self._holding_signal = False
                                self._receive_seconds_remaining = None
                            self._recovery_requested.clear()
                            consumer = Thread(target=self._run, name="sstv-decoder", daemon=True)
                            self._consumer_thread = consumer
                        if retrying:
                            self._publish_debug("warn", f"Restarting SSTV consumer (attempt {self._restart_count}): "
                                                f"{self._recovery_reason}. Queued audio and recordings retained.")
                        consumer.start()
                        interrupted = None
                        healthy_since = None
                else:
                    reason = self._watchdog_reason(now)
                    if reason and not self._recovery_requested.is_set():
                        with self._lock:
                            self._recovery_reason = reason
                            self._health = "recovering"
                        self._recovery_requested.set()
                        self._wake.set()
                        retry_at = now + retry_delay
                        retry_delay = min(_RETRY_MAX_S, retry_delay * 2)
                        LOGGER.error("SSTV watchdog: %s", reason)
                        self._publish_debug("error", f"Watchdog: {reason}; recovering automatically.")
                        self._publish_status()
                    if self._recovery_requested.is_set():
                        interrupted = self._interrupt_worker(interrupted)
                    elif not self._stop.is_set():
                        with self._lock:
                            if not self._radio_connected:
                                health = "waiting"
                            elif (self._worker_progress_at is not None
                                  and now - self._worker_progress_at < _WORKER_STALL_S
                                  and (self._worker_pending_since is not None or self._write_started_at is not None
                                       or self._last_packet_at is not None and now - self._last_packet_at < 3.0)):
                                health = "healthy"
                            elif (self._worker_ready and self._worker_pending_since is None
                                  and self._write_started_at is None):
                                health = "waiting"
                            else:
                                health = "starting"
                            changed = self._health != health
                            self._health = health
                        if changed:
                            self._publish_status()
                        if health == "healthy":
                            if healthy_since is None:
                                healthy_since = now
                            elif now - healthy_since >= 60.0:
                                retry_delay = 1.0
                        else:
                            healthy_since = None
                self._supervisor_wake.wait(_WATCHDOG_INTERVAL_S)
                self._supervisor_wake.clear()
        finally:
            self._consumer_thread = None
            if self._audio_controller is not None and self._audio_queue is not None:
                self._audio_controller.unsubscribe_audio(self._audio_queue)
            self._audio_controller = self._audio_queue = None
            self._audio_capture.close()

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
        self._audio_capture.append_decoder(pcm, sample_rate)

    def export_audio_capture(self, kind: str) -> Path | None:
        if kind == "metadata":
            return self._audio_capture.export_metadata()
        return self._audio_capture.export(kind)

    def decoder_audio_wav(self) -> bytes | None:
        path = self.export_audio_capture("decoder")
        if path is None:
            return None
        try:
            return path.read_bytes()
        finally:
            path.unlink(missing_ok=True)

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
        if missing_packets <= 0:
            return
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
            "reacquire",
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
        self._write_frame(frame_control({"command": "discontinuity", "lost_samples": None}))

    def _feed_audio_packets(self, packets: list[bytes], sample_rate: int, channels: int) -> int:
        wrote_samples = 0
        self._pending_packets.extend(packets)
        with self._lock:
            self._pending_batch_samples = sum(max(0, len(packet) - 24) // (2 * channels)
                                              for packet in self._pending_packets)
        while self._pending_packets and not self._recovery_requested.is_set():
            packet = self._pending_packets.popleft()
            pcm = self.extract_rx_pcm(packet, channels)
            if not pcm:
                self._publish_debug("warn", "Rejected a malformed SSTV audio packet.")
                continue
            gap = self._packet_gap(packet)
            gain = self._rx_gain_db
            input_level = self._pcm_rms_dbfs(pcm)
            stream = apply_gain_db(pcm, gain)
            peak = peak_of(stream)
            clipped = 0
            if gain > 0:
                values = array("h")
                values.frombytes(pcm)
                if sys.byteorder != "little":
                    values.byteswap()
                factor = 10.0 ** (gain / 20.0)
                clipped = sum(not -32768 <= int(value * factor) <= 32767 for value in values)
            with self._lock:
                self._last_packet_at = monotonic()
                self._input_level_dbfs = input_level
                self._decoder_peak = max(peak, int(self._decoder_peak * 0.9))
                self._clipped_samples += clipped
                radio_diagnostics = dict(self._radio_audio_diagnostics)
            self._audio_capture.append_source(pcm, sample_rate, {
                "consumer_at_monotonic": monotonic(),
                "local_sequence": self._packet_sequence(packet),
                "local_missing_packets": gap,
                "gain_db": gain,
                "source_peak_dbfs": dbfs_of(peak_of(pcm)),
                "source_rms_dbfs": input_level,
                "decoder_peak_dbfs": dbfs_of(peak),
                "gain_clipped_samples": clipped,
                "radio_audio_diagnostics": radio_diagnostics,
            })
            if gap:
                self._handle_audio_gap(gap)
            if self._write_pcm(stream):
                self._capture_decoder_pcm(stream, sample_rate)
                wrote_samples += len(stream) // 2
                with self._lock:
                    self._input_samples += len(stream) // 2
                    self._last_audio_at = monotonic()
            with self._lock:
                self._pending_batch_samples -= len(pcm) // 2
                failed = self._fatal_worker
            if failed:
                break
            capture_error = self._audio_capture.status()["error"]
            if capture_error and capture_error != self._reported_capture_error:
                self._reported_capture_error = capture_error
                self._publish_debug("error", capture_error)
                self._publish_status()
        return wrote_samples

    def _run(self) -> None:
        LOGGER.info("SSTV RX audio consumer thread started")
        controller = self._audio_controller
        audio_queue = self._audio_queue
        sample_rate = self._input_sample_rate or None
        channels = (2 if controller.config.rx_codec == "lpcm16_stereo" else 1) if controller else 0
        connected = False
        reported_connection: bool | None = None
        debug_window_started = monotonic()
        debug_packets = 0
        debug_samples = 0
        debug_queue_drops = 0
        last_input_rms_dbfs: float | None = None
        try:
            while not self._stop.is_set() and not self._recovery_requested.is_set():
                with self._lock:
                    self._consumer_progress_at = monotonic()
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
                        self._pending_packets.clear()
                        self._pending_batch_samples = 0
                        self._audio_controller = None
                        self._audio_queue = None
                        self._radio_connected = False
                        self._radio_audio_at = None
                        self._radio_audio_count = None
                    if controller is not None:
                        sample_rate = int(controller.config.sample_rate)
                        channels = 2 if controller.config.rx_codec == "lpcm16_stereo" else 1
                        audio_queue = controller.subscribe_audio(
                            self._wake.set,
                            max_seconds=None,
                            drop_oldest=False,
                        )
                        with self._lock:
                            self._audio_controller = controller
                            self._audio_queue = audio_queue
                            self._input_sample_rate = sample_rate
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
                            with self._lock:
                                self._radio_connected = connected
                                count = radio.get("rx_audio_packets")
                                if isinstance(count, int) and count != self._radio_audio_count:
                                    if self._radio_audio_count is not None:
                                        self._radio_audio_at = monotonic()
                                    self._radio_audio_count = count
                                self._radio_audio_diagnostics = {
                                    key: value for key, value in radio.items()
                                    if key.startswith("rx_audio_")
                                }
                    except Exception:
                        pass
                if connected != reported_connection:
                    if reported_connection is True and not connected:
                        self._terminate_worker()
                        with self._lock:
                            self._last_packet_sequence = None
                    LOGGER.info(
                        "SSTV radio connection %s",
                        "up" if connected else "down",
                    )
                    self._publish_debug(
                        "info" if connected else "warn",
                        "Radio connection is up." if connected else "Radio is disconnected; RX audio is unavailable.",
                    )
                    reported_connection = connected
                if connected and audio_queue is not None:
                    # Keep packets queued until a worker can accept them.
                    if not self._ensure_worker(int(sample_rate)):
                        with self._lock:
                            fatal_worker = self._fatal_worker
                        if fatal_worker:
                            break
                        self._wake.wait(0.5)
                        self._wake.clear()
                        continue
                packets = controller.read_audio(audio_queue) if connected and audio_queue is not None else []
                if packets or self._pending_packets:
                    debug_packets += len(packets)
                    wrote_samples = self._feed_audio_packets(packets, int(sample_rate), channels)
                    last_input_rms_dbfs = self._input_level_dbfs
                    if wrote_samples:
                        debug_samples += wrote_samples
                        with self._lock:
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
                debug_now = monotonic()
                debug_elapsed = debug_now - debug_window_started
                if debug_elapsed >= 5.0:
                    queue_drops = int(getattr(audio_queue, "dropped", 0))
                    pending_seconds = float(getattr(audio_queue, "pending_seconds", 0.0))
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
                            "sequence gaps=%d; local queue drops=%d; queued audio=%.2fs",
                            debug_packets,
                            debug_samples,
                            debug_elapsed,
                            sample_rate,
                            input_path,
                            status["decoder_level_dbfs"],
                            input_rms_text,
                            status["dropped_packets"],
                            queue_drops,
                            pending_seconds,
                        )
                        self._publish_debug(
                            "warn" if new_queue_drops or pending_seconds >= 1.0 else "info",
                            f"RX feed: {debug_packets} packets, {debug_samples} samples in {debug_elapsed:.1f}s; "
                            f"{sample_rate} Hz {input_path}; decoder peak {status['decoder_level_dbfs']} dBFS; "
                            f"latest packet RMS {input_rms_text} dBFS; "
                            f"sequence gaps total {status['dropped_packets']}; local queue drops "
                            f"{new_queue_drops} this interval/{queue_drops} total; "
                            f"queued audio {pending_seconds:.2f}s.",
                        )
                        if self._radio_audio_diagnostics:
                            diagnostics = self._radio_audio_diagnostics
                            self._publish_debug("info", "Radio audio sequence observations: "
                                f"{diagnostics.get('rx_audio_sequence_skipped', 0)} skipped slots; "
                                f"{diagnostics.get('rx_audio_duplicates', 0)} duplicates; "
                                f"{diagnostics.get('rx_audio_out_of_order', 0)} out of order.")
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
            try:
                if self._stop.is_set() and controller is not None and audio_queue is not None:
                    controller.unsubscribe_audio(audio_queue)
                    if not self._fatal_worker and not self._recovery_requested.is_set():
                        self._feed_audio_packets(controller.read_audio(audio_queue), int(sample_rate), channels)
                # A stale subscription must be replaced; ordinary worker failures
                # keep the subscription and unattempted packets for the retry.
                if (self._recovery_requested.is_set() and self._recovery_reason
                        == "SSTV subscription stopped receiving the radio audio stream"):
                    if controller is not None and audio_queue is not None:
                        controller.unsubscribe_audio(audio_queue)
                    with self._lock:
                        self._audio_queue = self._audio_controller = None
            finally:
                self._terminate_worker(drain=self._stop.is_set() and not self._fatal_worker
                                       and not self._recovery_requested.is_set())
                if self._thread is None:
                    self._audio_capture.close()

    def _decoder_command(self, sample_rate: int) -> list[str]:
        override = os.environ.get("PI_SAT_SSTV_DECODER")
        if override:
            # An override implements the same framed input/NDJSON output contract.
            return [str(Path(override).expanduser()), str(sample_rate)]
        return [sys.executable, "-u", "-m", "pi_sat_controller.backend.sstv.worker", str(sample_rate)]

    def _ensure_worker(self, sample_rate: int) -> bool:
        with self._lock:
            if self._fatal_worker or self._recovery_requested.is_set():
                return False
        with self._process_lock:
            exited = self._process is not None and self._process.poll() is not None
        if exited:
            self._terminate_worker()
        with self._process_lock:
            if self._closing_process is not None or self._stop.is_set():
                return False
            if self._process is not None and self._process.poll() is None:
                return True
            try:
                process = subprocess.Popen(
                    self._decoder_command(sample_rate), stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0, shell=False,
                )
            except OSError as exc:
                self._set_failure(f"Unable to start SSTVFlow worker: {exc}", fatal=True)
                return False
            self._process = process
            self._process_generation += 1
            generation = self._process_generation
            self._reader_done = Event()
            reader_done = self._reader_done
            self._last_worker_error = None
            self._fatal_worker = False
            with self._lock:
                self._worker_started_at = monotonic()
                self._worker_closing_at = None
                self._worker_ready = False
                self._worker_input_samples = 0
                self._worker_sample_position = 0
                self._worker_progress_at = None
                self._worker_pending_since = None
                self._write_started_at = None
        LOGGER.info("SSTVFlow worker started (%d Hz)", sample_rate)
        self._publish_debug("info", f"SSTVFlow worker started, input rate {sample_rate} Hz.")
        Thread(target=self._read_worker_events, args=(process, generation, reader_done),
               name="sstv-events", daemon=True).start()
        Thread(target=self._read_worker_errors, args=(process, generation),
               name="sstv-errors", daemon=True).start()
        return True

    def _write_pcm(self, pcm: bytes) -> bool:
        for start in range(0, len(pcm), MAX_FRAME_BYTES):
            chunk = pcm[start:start + MAX_FRAME_BYTES]
            if not self._write_frame(frame_pcm(chunk)):
                return False
            with self._lock:
                self._worker_input_samples += len(chunk) // 2
                if (self._worker_pending_since is None and self._input_sample_rate
                        and self._worker_input_samples - self._worker_sample_position >= self._input_sample_rate):
                    self._worker_pending_since = monotonic()
        return True

    def _write_frame(self, frame: bytes) -> bool:
        # Serialize PCM/control frames without blocking the stdout reader's lock.
        with self._write_lock:
            with self._process_lock:
                process = self._process
                if (process is None or process is self._closing_process or process.stdin is None
                        or process.poll() is not None):
                    return False
            try:
                with self._lock:
                    self._write_started_at = monotonic()
                remaining = memoryview(frame)
                while remaining:
                    written = process.stdin.write(remaining)
                    if not written:
                        raise BrokenPipeError("decoder input accepted no bytes")
                    remaining = remaining[written:]
                process.stdin.flush()
                return True
            except (OSError, ValueError) as exc:
                with self._process_lock:
                    active = process is self._process and process is not self._closing_process
                if active and not self._stop.is_set():
                    self._set_failure(f"SSTVFlow worker input failed: {exc}", fatal=True)
                return False
            finally:
                with self._lock:
                    self._write_started_at = None

    def _terminate_worker(self, *, drain: bool = False) -> None:
        with self._termination_lock:
            with self._process_lock:
                process = self._process
                if process is None:
                    return
                self._closing_process = process
                with self._lock:
                    self._worker_closing_at = monotonic()
                reader_done = self._reader_done
            graceful = self._write_lock.acquire(timeout=0.5)
            if graceful:
                try:
                    if process.stdin is not None:
                        process.stdin.close()
                except (OSError, ValueError):
                    pass
                finally:
                    self._write_lock.release()
            # A blocked writer means EOF cannot safely be delivered. Terminate
            # within a finite deadline rather than deadlocking radio disconnect.
            if process.poll() is None:
                try:
                    if not graceful:
                        process.terminate()
                    process.wait(timeout=None if drain and graceful else (3.0 if graceful else 1.0))
                except subprocess.TimeoutExpired:
                    process.terminate()
                    try:
                        process.wait(timeout=1.0)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=1.0)
                except ProcessLookupError:
                    pass
            reader_done.wait(timeout=None if drain else 2.0)
            if not reader_done.is_set():
                LOGGER.warning("SSTVFlow final event drain exceeded shutdown deadline")
            if not graceful and process.stdin is not None:
                try:
                    process.stdin.close()
                except (OSError, ValueError):
                    pass
            with self._process_lock:
                if self._process is process:
                    self._process = None
                    self._process_generation += 1
                    with self._lock:
                        self._worker_started_at = None
                        self._worker_closing_at = None
                        self._worker_ready = False
                        self._worker_progress_at = None
                        self._worker_pending_since = None
                self._closing_process = None

    def _read_worker_events(self, process: subprocess.Popen[bytes], generation: int, reader_done: Event | None = None) -> None:
        if process.stdout is None:
            if reader_done is not None:
                reader_done.set()
            return
        adapter = SstvEventAdapter()
        try:
            for raw_line in iter(process.stdout.readline, b""):
                with self._process_lock:
                    if generation != self._process_generation or process is not self._process:
                        return
                try:
                    event = json.loads(raw_line)
                    if not isinstance(event, dict):
                        raise ValueError("worker event is not an object")
                    event = adapter.adapt(event)
                    if event is not None:
                        with self._process_lock:
                            if generation != self._process_generation or process is not self._process:
                                return
                            self._record_worker_progress(event)
                        self._handle_worker_event(event)
                except Exception as exc:
                    LOGGER.warning("Ignored invalid SSTVFlow worker event: %s", exc)
                    self._set_failure(f"SSTVFlow event handling failed: {exc}", fatal=False)
        finally:
            process.stdout.close()
            with self._process_lock:
                active = generation == self._process_generation and process is self._process
                if active and process is not self._closing_process and not self._stop.is_set():
                    exit_code = process.poll()
                    with self._lock:
                        worker_error = self._last_worker_error
                    message = f"SSTVFlow worker exited unexpectedly (code {exit_code})"
                    if worker_error:
                        message = f"{message}: {worker_error}"
                    self._set_failure(message, fatal=True)
            if reader_done is not None:
                reader_done.set()

    def _record_worker_progress(self, event: dict[str, Any]) -> None:
        with self._lock:
            if event.get("type") == "ready":
                self._worker_ready = True
            position = event.get("sample_position")
            if (event.get("type") not in {"quality", "status"} or type(position) is not int
                    or position <= self._worker_sample_position):
                return
            self._worker_sample_position = position
            self._worker_progress_at = monotonic()
            self._worker_pending_since = (
                self._worker_progress_at if self._input_sample_rate
                and self._worker_input_samples - position >= self._input_sample_rate else None)
            if not self._recovery_requested.is_set():
                self._health = "healthy"

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
                    LOGGER.warning("SSTVFlow: %s", message)
                    self._publish_debug("warn", f"SSTVFlow stderr: {message}")
        finally:
            process.stderr.close()

    def _handle_worker_event(self, event: dict[str, Any]) -> None:
        event_type = event.get("type")
        if event_type == "ready":
            modes = ", ".join(SSTV_MODE_DIMENSIONS)
            LOGGER.info("SSTVFlow worker ready; scanning for %s", modes)
            self._publish_debug("info", f"SSTVFlow ready; scanning for {modes} headers and line structure.")
            return
        if event_type in {"status", "quality", "acquisition"}:
            offset = event.get("frequency_offset_hz")
            holding = event.get("holding_signal")
            remaining = event.get("receive_seconds_remaining")
            with self._lock:
                if isinstance(offset, (int, float)) and math.isfinite(offset):
                    self._frequency_offset_hz = float(offset)
                if isinstance(holding, bool):
                    self._holding_signal = holding
                if "receive_seconds_remaining" in event:
                    # The window ends when reception does; the decoder reports null then.
                    self._receive_seconds_remaining = (
                        float(remaining)
                        if isinstance(remaining, (int, float)) and math.isfinite(remaining)
                        else None
                    )
            self._publish({"type": "decoder_status", "decoder_event": event})
            self._publish_status()
            return
        if event_type in {"warning", "error"}:
            message = str(event.get("message") or event.get("code") or "Decoder warning")
            self._publish_debug("error" if event_type == "error" else "warn", message)
            if event_type == "error":
                with self._lock:
                    self._last_worker_error = message[-512:]
                self._set_failure(message, fatal=event.get("code") == "worker_failed")
            return
        if event_type == "vis_detected":
            width = int(event["width"])
            height = int(event["height"])
            if not 0 < width <= 2048 or not 0 < height <= 2048:
                raise ValueError("SSTVFlow returned invalid image dimensions")
            with self._lock:
                self._mode = str(event["mode"])
                self._line = 0
                self._total_lines = height
                self._frequency_offset_hz = float(event["frequency_offset_hz"])
                self._holding_signal = False
                self._receive_seconds_remaining = None
                self._capture_context = dict(self.get_context())
                self._current = {
                    "image_id": event.get("image_id"),
                    "acquisition": event.get("acquisition"),
                    "mode": self._mode,
                    "width": width,
                    "height": height,
                    "last_line": 0,
                    "quality": "degraded" if event.get("partial") else "clean",
                    "interference_packets": 0,
                    "pixels": bytearray(width * height * 3),
                    "received_lines": set(),
                }
            LOGGER.info(
                "SSTV acquired: %s %dx%d at %+.1f Hz",
                self._mode,
                width,
                height,
                self._frequency_offset_hz,
            )
            self._set_state(self.DETECTED)
            self._publish_debug(
                "info",
                f"SSTV acquired: {self._mode}, {width}x{height}, "
                f"frequency offset {self._frequency_offset_hz:+.1f} Hz.",
            )
            self._publish(
                {
                    "type": "image_started",
                    "mode": self._mode,
                    "width": width,
                    "height": height,
                    "frequency_offset_hz": self._frequency_offset_hz,
                    "partial": bool(event.get("partial")),
                    "acquisition": event.get("acquisition"),
                    "image_id": event.get("image_id"),
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
                raise ValueError("SSTVFlow line buffer length is invalid")
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
                    "SSTVFlow line %d discarded by backend: %s",
                    line_index + 1,
                    drop_reason,
                )
                self._publish_debug(
                    "error",
                    f"SSTVFlow emitted line {line_index + 1}, but Pi-Sat discarded it: {drop_reason}.",
                )
                return
            LOGGER.debug("SSTVFlow line %d/%d accepted", line_index + 1, line_height)
            self._publish_debug(
                "info",
                f"SSTVFlow emitted line {line_index + 1}/{line_height}; Pi-Sat accepted and forwarding it to the page.",
            )
            self._set_state(self.DECODING)
            self._publish(
                {
                    "type": "line_decoded",
                    "line_index": line_index,
                    "width": width,
                    "rgb_base64": event["rgb_base64"],
                    "height": line_height,
                    "mode": self._mode,
                    "row_revision": event.get("row_revision", False),
                    "revision": event.get("revision", 0),
                    "image_id": event.get("image_id"),
                }
            )
            return
        if event_type == "image_complete":
            width = int(event["width"])
            height = int(event["height"])
            rgb = base64.b64decode(str(event["rgb_base64"]), validate=True)
            if len(rgb) != width * height * 3:
                raise ValueError("SSTVFlow image buffer length is invalid")
            with self._lock:
                mode = str(event["mode"])
                previous_quality = str(self._current.get("quality", "clean")) if self._current else "clean"
                previous_interference = int(self._current.get("interference_packets", 0)) if self._current else 0
                progressive = bytes(self._current.get("pixels", b"")) if self._current else None
                received_lines = set(self._current.get("received_lines", set())) if self._current else set()
                self._mode = mode
                self._line = height
                self._total_lines = height
                self._holding_signal = False
                self._receive_seconds_remaining = None
                self._current = {
                    "mode": mode,
                    "width": width,
                    "height": height,
                    "last_line": height,
                    "quality": "degraded" if event.get("quality") == "degraded" or event.get("partial", False) or previous_quality != "clean" else "clean",
                    "image_id": event.get("image_id"),
                    "acquisition": event.get("acquisition"),
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
                png=event.get("png_bytes"),
                acquisition=event.get("acquisition"),
                reception=event.get("reception"),
            )
            self._notify_image_saved(metadata)
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
            self._publish({"type": "image_complete", "source": "radio", "image": metadata,
                           "degraded": quality != "clean"})

    def _notify_image_saved(self, metadata: dict[str, Any]) -> None:
        if self.on_image_saved is None:
            return
        capture_id = str(metadata.get("id", ""))
        image_path = self.gallery.image_path(capture_id)
        if image_path is None:
            LOGGER.error("Saved SSTV image could not be found for notification")
            return
        try:
            self.on_image_saved(image_path, metadata)
        except Exception:
            LOGGER.exception("Unable to enqueue the saved SSTV image notification")

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
