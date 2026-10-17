from __future__ import annotations

import array
import logging
import os
import select
import subprocess
import threading
import time
from collections.abc import Callable
from typing import Any

LOGGER = logging.getLogger(__name__)


class WsjtxTxAudioBridge:
    """Deliver the managed decoder's isolated output through the shared radio."""

    def __init__(self, *, get_controller: Callable[[], Any], get_status: Callable[[], dict],
                 get_block_reason: Callable[[], str], capture_command: Callable[[int], list[str]],
                 get_environment: Callable[[], dict[str, str]], request_status: Callable[[], Any] | None = None) -> None:
        self._get_controller = get_controller
        self._get_status = get_status
        self._get_block_reason = get_block_reason
        self._capture_command = capture_command
        self._get_environment = get_environment
        self._request_status = request_status
        self._lock = threading.RLock()
        self._operation = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._process: subprocess.Popen | None = None
        self._controller = None
        self._token: str | None = None
        self._enabled = False
        self._transmitting = False
        self._rate = 0
        self._frames = 0
        self._sequence = 0
        self._message = ""
        self._prepared_message = ""
        self._unmatched_since: float | None = None
        self._status_requested_at = 0.0
        self._state = "stopped"
        self._detail = ""
        self._keyed_at = 0.0
        self._tail_until: float | None = None
        self._radio_state: dict = {}
        self._radio_seen_at = 0.0

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {"state": self._state, "detail": self._detail, "enabled": self._enabled,
                    "transmitting": self._transmitting, "message": self._message,
                    "sample_rate": self._rate, "frames": self._frames, "sequence": self._sequence,
                    "capture_running": self._process is not None and self._process.poll() is None,
                    "owns_transmit": self._token is not None}

    def prepare(self, message: str) -> None:
        with self._lock:
            if self._transmitting:
                raise RuntimeError("Wait for the current native transmission to finish.")
            self._prepared_message = message
            self._unmatched_since = None

    def _radio(self, controller: Any) -> dict:
        result = controller.try_snapshot() if controller is not None else None
        if isinstance(result, dict):
            self._radio_state, self._radio_seen_at = result, time.monotonic()
            return result
        if controller is self._controller and time.monotonic() - self._radio_seen_at < 2:
            return self._radio_state
        return {}

    def _check(self, controller: Any) -> None:
        reason = self._get_block_reason()
        if reason:
            raise RuntimeError(reason)
        if controller is not self._get_controller() or not self._radio(controller).get("connected"):
            raise RuntimeError("Native radio disconnected or changed; TX stopped.")
        if int(controller.config.sample_rate) != self._rate:
            raise RuntimeError("Native radio audio format changed; TX stopped.")
        if controller.config.tx_codec not in ("lpcm16_mono", "lpcm16_stereo"):
            raise RuntimeError("Native radio transmit audio requires 16-bit LPCM.")

    def start(self, sample_rate: int) -> None:
        self.stop()
        if type(sample_rate) is not int or not 8000 <= sample_rate <= 192000:
            raise ValueError("Invalid native transmit sample rate")
        process = subprocess.Popen(self._capture_command(sample_rate), stdout=subprocess.PIPE,
                                   stdin=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                   bufsize=0, env=self._get_environment())
        assert process.stdout is not None
        os.set_blocking(process.stdout.fileno(), False)
        with self._lock:
            self._stop = threading.Event()
            self._process, self._rate = process, sample_rate
            self._frames = 0
            self._state, self._detail = "listening", "Managed TX output is isolated; transmitter is disabled."
            self._thread = threading.Thread(target=self._run, name="wsjtx-tx-audio", daemon=True)
            self._thread.start()

    def set_enabled(self, enabled: bool) -> None:
        with self._operation:
            if not enabled:
                self._disable()
                return
            with self._lock:
                if self._enabled:
                    return
                if self._process is None or self._process.poll() is not None or self._state == "error":
                    raise RuntimeError("Managed transmit audio capture is unavailable.")
            controller = self._get_controller()
            self._check(controller)
            if not callable(getattr(controller, "claim_transmit_stream", None)):
                raise RuntimeError("Native radio digital transmit ownership is unavailable.")
            token = controller.claim_transmit_stream("wsjtx")
            try:
                state = controller.configure_microphone(use_transport=True) or {}
                microphone = state.get("microphone", {})
                if microphone.get("lan_mod_level", 0) <= 0:
                    raise RuntimeError("Radio LAN modulation level is zero or unavailable.")
                # The controller verifies the transport input when configuring it.
                self._check(controller)
                with self._lock:
                    self._controller, self._token = controller, token
                    self._enabled = True
                    self._state, self._detail = "armed", "Waiting for the selected WSJT-X transmit slot."
            except Exception:
                controller.release_transmit_stream(token)
                raise

    def _release_key(self) -> None:
        with self._lock:
            controller, token = self._controller, self._token
        if controller is None or token is None:
            return
        problem = "Radio transmit release was not confirmed."
        for _ in range(3):
            try:
                # An operator unkey can revoke the token before this worker
                # cleans up. Confirm an idle radio without reusing that token.
                if controller.transmit_active or not controller.wait_for_ptt(False, 0):
                    controller.set_ptt(False, transmit_token=token)
                if controller.wait_for_ptt(False, 0.8):
                    with self._lock:
                        self._transmitting = False
                        self._tail_until = None
                        self._sequence += 1
                    return
            except Exception as error:
                problem = str(error)
            if not self._radio(controller).get("connected"):
                break
        raise RuntimeError(f"WSJT-X could not confirm PTT OFF: {problem}")

    def _disable(self) -> None:
        with self._lock:
            self._enabled = False
            controller, token = self._controller, self._token
            keyed = self._transmitting
        if controller is not None and token is not None:
            if keyed or controller.transmit_active:
                self._release_key()
            controller.release_transmit_stream(token)
        with self._lock:
            self._controller = self._token = None
            self._transmitting = False
            self._tail_until = None
            if self._state != "error":
                self._state, self._detail = "listening", "Transmitter is disabled."

    def _update_intent(self, status: dict, now: float) -> None:
        with self._lock:
            enabled, keyed = self._enabled, self._transmitting
            controller, token = self._controller, self._token
        if not enabled:
            return
        self._check(controller)
        if not status.get("connected"):
            raise RuntimeError("Managed WSJT-X status is unavailable; transmitter stopped.")
        intent = status.get("transmitting") is True
        if intent and not keyed:
            if self._prepared_message and str(status.get("tx_message", "")).strip() != self._prepared_message:
                self._unmatched_since = self._unmatched_since or now
                if self._request_status is not None and now - self._status_requested_at >= 0.3:
                    self._request_status()
                    self._status_requested_at = now
                if now - self._unmatched_since > 0.4:
                    raise RuntimeError(f"Stock transmit message {status.get('tx_message', '')!r} does not match {self._prepared_message!r}; PTT was not keyed.")
                return
            self._unmatched_since = None
            controller.set_transmit_stream_paced(True, transmit_token=token)
            controller.set_ptt(True, transmit_token=token)
            with self._lock:
                self._transmitting = True
                self._keyed_at = time.monotonic()
                self._tail_until = None
                self._message = str(status.get("tx_message", ""))
                self._sequence += 1
                self._state, self._detail = "transmitting", self._message
            LOGGER.info("WSJT-X native transmit started: %s", self._message)
        elif keyed:
            if intent:
                self._tail_until = None
            maximum = 8.5 if status.get("mode") == "FT4" else 16.0
            if now - self._keyed_at > maximum:
                raise RuntimeError("WSJT-X transmit exceeded its slot; transmitter stopped.")
            if not intent and self._tail_until is None:
                self._tail_until = now + 0.15
            if self._tail_until is not None and now >= self._tail_until:
                controller.flush_audio(transmit_token=token)
                self._release_key()
                with self._lock:
                    self._state, self._detail = "armed", "Waiting for the next transmit slot."

    def _run(self) -> None:
        import fcntl
        import termios

        process = self._process
        assert process is not None and process.stdout is not None
        descriptor = process.stdout.fileno()
        partial = bytearray()
        last_pcm = time.monotonic()
        try:
            while not self._stop.is_set():
                with self._operation:
                    self._update_intent(self._get_status(), time.monotonic())
                if process.poll() is not None:
                    raise RuntimeError("Managed transmit audio capture exited.")
                ready, _, _ = select.select([descriptor], [], [], 0.02)
                if not ready:
                    if self._transmitting and time.monotonic() - max(last_pcm, self._keyed_at) > 0.6:
                        raise RuntimeError("Managed transmit audio stalled; transmitter stopped.")
                    continue
                queued = array.array("i", [0])
                fcntl.ioctl(descriptor, termios.FIONREAD, queued, True)
                if self._transmitting and queued[0] > self._rate * 2 * 0.4:
                    raise RuntimeError("Managed transmit audio backlog exceeded 400 ms; transmitter stopped.")
                chunk = os.read(descriptor, max(2, self._rate // 50 * 2))
                if not chunk:
                    raise RuntimeError("Managed transmit audio capture closed.")
                last_pcm = time.monotonic()
                partial.extend(chunk)
                count = len(partial) // 2 * 2
                pcm = bytes(partial[:count])
                del partial[:count]
                with self._operation:
                    if self._enabled and self._transmitting and pcm:
                        self._controller.send_audio(pcm, transmit_token=self._token)
                        with self._lock:
                            self._frames += len(pcm) // 2
        except Exception as error:
            with self._lock:
                self._state, self._detail = "error", str(error)
            LOGGER.error("WSJT-X transmit stopped: %s", error)
        finally:
            with self._operation:
                try:
                    self._disable()
                except Exception as error:
                    with self._lock:
                        self._state = "error"
                        self._detail = "; ".join(filter(None, (self._detail, str(error))))
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=1)

    def stop(self) -> dict[str, Any]:
        self._stop.set()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=4)
            if thread.is_alive():
                raise RuntimeError("Managed transmit shutdown did not finish; check radio PTT.")
        process = self._process
        if process is not None:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=2)
            if process.stdout is not None:
                process.stdout.close()
        cleanup_error: Exception | None = None
        with self._operation:
            # The worker may have recorded a stall after releasing PTT. Retry the
            # release with the original claim so a resolved condition clears it
            # while a still-unconfirmed release keeps the claim for retry.
            try:
                self._disable()
            except Exception as error:
                cleanup_error = error
        with self._lock:
            self._thread = self._process = None
            if cleanup_error is not None:
                self._state = "error"
                self._detail = "; ".join(filter(None, (self._detail, str(cleanup_error))))
            elif self._state == "error":
                # Resources are released now. Keep the reason the capture ended,
                # but this is a completed cleanup rather than a live failure.
                self._state = "stopped"
            else:
                self._state, self._detail = "stopped", "Managed transmit audio capture is stopped."
            return self.snapshot()
