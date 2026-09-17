from __future__ import annotations

from array import array
import base64
from collections.abc import Callable
import json
import logging
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


LOGGER = logging.getLogger(__name__)


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
        # Level of the trimmed stream actually handed to the decoder.
        self._decoder_peak = 0
        self._rx_gain_db = clamp_gain_db(rx_gain_db)
        self._last_audio_at: float | None = None
        self._terminal_until = 0.0
        self._current: dict[str, Any] | None = None
        self._capture_context: dict[str, Any] = {}
        self._subscribers: list[Queue[dict[str, Any]]] = []
        self._sequence = 0
        self._gallery_count = len(self.gallery.list())
        self._stop = Event()
        self._wake = Event()
        self._thread: Thread | None = None
        self._process: subprocess.Popen[bytes] | None = None
        self._process_generation = 0
        self._fatal_worker = False

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
                "gallery_count": self._gallery_count,
                "audio_source": "Native IC-9700 SUB/RX PCM",
                "decoder": "slowrx.rs 0.5.3",
                "current_image": current,
                "sequence": self._sequence,
            }

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

    def set_enabled(self, enabled: bool) -> dict[str, Any]:
        if type(enabled) is not bool:
            raise ValueError("enabled must be a boolean")
        with self._lifecycle_lock:
            if enabled:
                with self._lock:
                    if self._enabled:
                        return self.snapshot()
                    self._enabled = True
                    self._state = self.WAITING
                    self._error = None
                    self._fatal_worker = False
                    self._stop.clear()
                    self._thread = Thread(target=self._run, name="sstv-decoder", daemon=True)
                    thread = self._thread
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
                    thread = self._thread
                    self._thread = None
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

    def _run(self) -> None:
        controller = None
        audio_queue = None
        sample_rate = None
        channels = 0
        connected = False
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
                    if controller is not None:
                        sample_rate = int(controller.config.sample_rate)
                        channels = 2 if controller.config.rx_codec == "lpcm16_stereo" else 1
                        audio_queue = controller.subscribe_audio(self._wake.set)

                if controller is not None:
                    try:
                        snapshot_reader = getattr(controller, "try_snapshot", None)
                        radio = snapshot_reader() if snapshot_reader is not None else controller.snapshot()
                        if radio is not None:
                            connected = bool(radio.get("connected"))
                    except Exception:
                        pass
                packets = controller.read_audio(audio_queue) if connected and audio_queue is not None else []
                if packets:
                    if not self._ensure_worker(int(sample_rate)):
                        self._wake.wait(0.5)
                        self._wake.clear()
                        continue
                    wrote_samples = 0
                    for packet in packets:
                        pcm = self.extract_rx_pcm(packet, channels)
                        if not pcm:
                            continue
                        stream = apply_gain_db(pcm, self._rx_gain_db)
                        with self._lock:
                            self._decoder_peak = max(peak_of(stream), int(self._decoder_peak * 0.9))
                        if self._write_pcm(stream):
                            wrote_samples += len(stream) // 2
                    if wrote_samples:
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
                self.project_root / "sstv_decoder" / "target" / "release" / f"pi-sat-sstv-decoder{suffix}",
                self.project_root / "sstv_decoder" / "target" / "debug" / f"pi-sat-sstv-decoder{suffix}",
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
                    "slowrx worker is not built; run cargo build --release --locked --manifest-path sstv_decoder/Cargo.toml",
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
            try:
                process.stdin.write(pcm)
                process.stdin.flush()
                return True
            except (BrokenPipeError, OSError) as exc:
                self._set_failure(f"slowrx worker input failed: {exc}", fatal=True)
                return False

    def _terminate_worker(self) -> None:
        with self._process_lock:
            process = self._process
            self._process = None
            self._process_generation += 1
        if process is None:
            return
        if process.stdin is not None:
            try:
                process.stdin.close()
            except OSError:
                pass
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=1.0)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=1.0)

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
            with self._process_lock:
                active = generation == self._process_generation and process is self._process
            if active and not self._stop.is_set():
                self._set_failure("slowrx worker exited unexpectedly", fatal=True)

    def _read_worker_errors(self, process: subprocess.Popen[bytes], generation: int) -> None:
        if process.stderr is None:
            return
        for raw_line in iter(process.stderr.readline, b""):
            with self._process_lock:
                if generation != self._process_generation or process is not self._process:
                    return
            message = raw_line.decode("utf-8", errors="replace").strip()
            if message:
                LOGGER.warning("slowrx: %s", message)

    def _handle_worker_event(self, event: dict[str, Any]) -> None:
        with self._lock:
            if not self._enabled:
                return
        event_type = event.get("type")
        if event_type == "ready":
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
                    "pixels": bytearray(width * height * 3),
                }
            self._set_state(self.DETECTED)
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
            self._set_failure(f"Unknown SSTV VIS code {event.get('code')}", fatal=False)
            return
        if event_type == "line_decoded":
            rgb = base64.b64decode(str(event["rgb_base64"]), validate=True)
            line_index = int(event["line_index"])
            width = int(event["width"])
            if len(rgb) != width * 3:
                raise ValueError("slowrx line buffer length is invalid")
            with self._lock:
                current = self._current
                if (
                    current is None
                    or width != current["width"]
                    or not 0 <= line_index < current["height"]
                ):
                    return
                start = line_index * width * 3
                current["pixels"][start : start + len(rgb)] = rgb
                current["last_line"] = max(current["last_line"], line_index + 1)
                self._line = current["last_line"]
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
                self._mode = mode
                self._line = height
                self._total_lines = height
                self._current = {
                    "mode": mode,
                    "width": width,
                    "height": height,
                    "last_line": height,
                    "pixels": bytearray(rgb),
                }
                context = dict(self._capture_context or self.get_context())
            metadata = self.gallery.save(
                mode=mode,
                width=width,
                height=height,
                rgb=rgb,
                partial=bool(event.get("partial", False)),
                context=context,
            )
            with self._lock:
                self._gallery_count = len(self.gallery.list())
                self._terminal_until = monotonic() + 3.0
            self._set_state(self.COMPLETE)
            self._publish({"type": "image_complete", "image": metadata})

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
