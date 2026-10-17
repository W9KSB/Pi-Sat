"""Bounded native receive-audio feed for a local WSJT-X playback device."""

from __future__ import annotations

import os
import select
import subprocess
import threading
import time
from array import array
from collections.abc import Callable
from typing import Any

from pi_sat_controller.backend.radio.audio_stream_protocol import extract_mono_pcm

_QUEUE_SECONDS = 2.0
_WRITE_TIMEOUT_SECONDS = 1.0
_PRIME_SECONDS = 2.0
_PRIME_TIMEOUT_SECONDS = 6.0


class _Stopped(Exception):
    pass


class WsjtxRxAudioBridge:
    """Forward subscribed AF PCM to a supplied local playback command."""

    def __init__(
        self,
        *,
        get_controller: Callable[[], Any],
        playback_command: Callable[[int], list[str]],
        get_block_reason: Callable[[], str] = lambda: "",
        get_playback_environment: Callable[[], dict[str, str]] | None = None,
        on_pcm: Callable[[bytes, int], None] | None = None,
        channel: str = "right",
    ) -> None:
        if channel not in ("left", "right"):
            raise ValueError("WSJT-X RX channel must be left or right")
        self._get_controller = get_controller
        self._playback_command = playback_command
        self._get_block_reason = get_block_reason
        self._get_playback_environment = get_playback_environment
        self._on_pcm = on_pcm
        self._channel = channel
        self._lock = threading.RLock()
        self._lifecycle_lock = threading.Lock()
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None
        self._process: subprocess.Popen | None = None
        self._audio_queue: Any = None
        self._state = "stopped"
        self._detail = ""
        self._sample_rate = 0
        self._source_bytes = 0
        self._dropped_packets = 0
        self._pending_bytes = 0

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            pending = self._audio_queue.pending_seconds if self._audio_queue is not None else 0.0
            return {
                "running": self._state == "running",
                "state": self._state,
                "detail": self._detail,
                "sample_rate": self._sample_rate,
                "source_frames": self._source_bytes // 2,
                "dropped_packets": max(self._dropped_packets, self._audio_queue.dropped if self._audio_queue is not None else 0),
                "queue_seconds": round(pending + self._pending_bytes / max(1, self._sample_rate * 2), 3),
                "source": "Pi-Sat SUB AF" if self._channel == "right" else "Pi-Sat MAIN AF",
                "playback_running": self._process is not None and self._process.poll() is None,
            }

    def start(self) -> dict[str, Any]:
        with self._lifecycle_lock:
            with self._lock:
                if self._thread is not None and self._thread.is_alive():
                    return self.snapshot()
                self._stop.clear()
                self._wake.clear()
                self._state = "waiting"
                self._detail = "Waiting for the native radio receive-audio source"
                self._sample_rate = self._source_bytes = self._dropped_packets = self._pending_bytes = 0
                worker = threading.Thread(target=self._run, name="wsjtx-rx-audio", daemon=True)
                try:
                    worker.start()
                    self._thread = worker
                except Exception as exc:
                    self._state = "error"
                    self._detail = f"Could not start WSJT-X RX audio bridge: {exc}"
                return self.snapshot()

    def stop(self) -> dict[str, Any]:
        with self._lifecycle_lock:
            self._stop.set()
            self._wake.set()
            with self._lock:
                worker = self._thread
            if worker is not None:
                worker.join(timeout=2.5)
            with self._lock:
                if worker is not None and worker.is_alive():
                    self._state = "error"
                    self._detail = "WSJT-X RX audio bridge did not stop in time"
                else:
                    self._thread = None
                    self._state = "stopped"
                    self._detail = ""
                return self.snapshot()

    @staticmethod
    def _radio_snapshot(controller: Any) -> dict[str, Any] | None:
        reader = getattr(controller, "try_snapshot", None)
        return reader() if reader is not None else controller.snapshot()

    @staticmethod
    def _format(controller: Any) -> tuple[int, str, int]:
        rate = controller.config.sample_rate
        codec = controller.config.rx_codec
        if type(rate) is not int or rate <= 0 or codec not in ("lpcm16_stereo", "lpcm16_mono"):
            raise RuntimeError("Native radio RX audio format is unsupported")
        return rate, codec, 2 if codec == "lpcm16_stereo" else 1

    def _check_source(self, controller: Any, audio_queue: Any, audio_format: tuple[int, str, int], process: subprocess.Popen) -> None:
        if self._stop.is_set():
            raise _Stopped()
        reason = self._get_block_reason()
        if reason:
            raise RuntimeError(reason)
        if self._get_controller() is not controller or self._format(controller) != audio_format:
            raise RuntimeError("Native radio receive-audio source changed; restart the WSJT-X module")
        radio = self._radio_snapshot(controller)
        if radio is not None and not radio.get("connected"):
            raise RuntimeError("Native radio disconnected; WSJT-X RX audio stopped")
        if audio_queue is not None:
            with self._lock:
                self._dropped_packets = audio_queue.dropped
            if audio_queue.overrun or audio_queue.dropped:
                raise BufferError("Native RX audio exceeded the two-second buffer; WSJT-X RX audio stopped")
        exit_code = process.poll()
        if exit_code is not None:
            raise RuntimeError(f"WSJT-X audio playback process exited ({exit_code})")

    def _write_pcm(self, pcm: bytes, controller: Any, audio_queue: Any, audio_format: tuple[int, str, int], process: subprocess.Popen) -> None:
        assert process.stdin is not None
        fd = process.stdin.fileno()
        remaining = memoryview(pcm)
        deadline = time.monotonic() + _WRITE_TIMEOUT_SECONDS
        while remaining:
            self._check_source(controller, audio_queue, audio_format, process)
            if time.monotonic() >= deadline:
                raise TimeoutError("WSJT-X audio playback stopped accepting PCM")
            _, ready, _ = select.select([], [fd], [], 0.05)
            if not ready:
                continue
            try:
                written = os.write(fd, remaining[:65536])
            except BlockingIOError:
                continue
            if written <= 0:
                raise BrokenPipeError("WSJT-X audio playback pipe closed")
            remaining = remaining[written:]
            with self._lock:
                self._source_bytes += written
                self._pending_bytes -= written
                self._state = "running"
                self._detail = "Forwarding native AF audio to the local decoder input"
            deadline = time.monotonic() + _WRITE_TIMEOUT_SECONDS

    def _prime_playback(self, controller: Any, audio_format: tuple[int, str, int], process: subprocess.Popen) -> None:
        # Wake and drain playback before accepting real samples. Otherwise a
        # sink's initial scheduling delay becomes persistent receive latency.
        import fcntl
        import termios

        assert process.stdin is not None
        fd = process.stdin.fileno()
        sample_rate = audio_format[0]
        remaining = int(sample_rate * _PRIME_SECONDS) * 2
        silence = bytes(max(2, int(sample_rate * 0.02) * 2))
        deadline = time.monotonic() + _PRIME_TIMEOUT_SECONDS
        while remaining:
            self._check_source(controller, None, audio_format, process)
            if time.monotonic() >= deadline:
                raise TimeoutError("WSJT-X audio playback did not finish priming")
            _, ready, _ = select.select([], [fd], [], 0.05)
            if not ready:
                continue
            try:
                written = os.write(fd, silence[:remaining])
            except BlockingIOError:
                continue
            if written <= 0:
                raise BrokenPipeError("WSJT-X audio playback pipe closed")
            remaining -= written
        while True:
            self._check_source(controller, None, audio_format, process)
            pending = array("i", [0])
            fcntl.ioctl(fd, termios.FIONREAD, pending, True)
            if pending[0] == 0:
                return
            if time.monotonic() >= deadline:
                raise TimeoutError("WSJT-X audio playback did not drain its startup buffer")
            self._stop.wait(0.02)

    @staticmethod
    def _terminate(process: subprocess.Popen | None) -> None:
        if process is None:
            return
        if process.stdin is not None:
            process.stdin.close()
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=0.5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=0.5)
        else:
            process.wait()

    def _run(self) -> None:
        controller = audio_queue = process = None
        try:
            while not self._stop.is_set():
                reason = self._get_block_reason()
                if reason:
                    raise RuntimeError(reason)
                candidate = self._get_controller()
                radio = self._radio_snapshot(candidate) if candidate is not None else None
                if candidate is None or radio is None or not radio.get("connected"):
                    self._wake.wait(0.1)
                    self._wake.clear()
                    continue
                controller = candidate
                audio_format = self._format(controller)
                sample_rate, _, channels = audio_format
                command = self._playback_command(sample_rate)
                if not isinstance(command, list) or not command or any(not isinstance(arg, str) or not arg for arg in command):
                    raise ValueError("WSJT-X audio playback command must be a nonempty argument list")
                with self._lock:
                    self._sample_rate = sample_rate
                environment = self._get_playback_environment() if self._get_playback_environment is not None else None
                process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, bufsize=0, env=environment)
                assert process.stdin is not None
                os.set_blocking(process.stdin.fileno(), False)
                with self._lock:
                    self._process = process
                    self._detail = "Priming local audio playback before subscribing to native AF PCM"
                self._prime_playback(controller, audio_format, process)
                audio_queue = controller.subscribe_audio(self._wake.set, max_seconds=_QUEUE_SECONDS, drop_oldest=False)
                with self._lock:
                    self._audio_queue = audio_queue
                    self._detail = "Local audio playback started; waiting for native AF PCM"
                while not self._stop.is_set():
                    self._check_source(controller, audio_queue, audio_format, process)
                    packets = controller.read_audio(audio_queue)
                    self._check_source(controller, audio_queue, audio_format, process)
                    with self._lock:
                        self._pending_bytes = sum(max(0, len(packet) - 24) // channels for packet in packets)
                    for packet in packets:
                        pcm = extract_mono_pcm(packet, channels, self._channel)
                        if not pcm:
                            raise ValueError("Malformed native RX audio packet; WSJT-X RX audio stopped")
                        if self._on_pcm is not None:
                            self._on_pcm(pcm, sample_rate)
                        self._write_pcm(pcm, controller, audio_queue, audio_format, process)
                    if not packets:
                        self._wake.wait(0.05)
                        self._wake.clear()
                break
        except _Stopped:
            pass
        except Exception as exc:
            with self._lock:
                self._state = "error"
                self._detail = str(exc) or "WSJT-X RX audio bridge failed"
        finally:
            if controller is not None and audio_queue is not None:
                try:
                    controller.unsubscribe_audio(audio_queue)
                except Exception:
                    pass
            try:
                self._terminate(process)
            except (OSError, subprocess.TimeoutExpired):
                pass
            with self._lock:
                if audio_queue is not None:
                    self._dropped_packets = audio_queue.dropped
                self._audio_queue = self._process = None
                self._pending_bytes = 0
                if self._stop.is_set():
                    self._state = "stopped"
                    self._detail = ""
