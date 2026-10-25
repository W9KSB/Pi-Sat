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


class WsjtxTxAudioError(RuntimeError):
    """A classified managed-transmit failure for fault containment."""

    def __init__(self, message: str, *, category: str) -> None:
        super().__init__(message)
        self.category = category


class WsjtxTxAudioBridge:
    """Deliver the managed decoder's isolated output through the shared radio."""

    _SLOT_PERIODS_S = {"FT4": 7.5, "FT8": 15.0}
    _SLOT_RELEASE_GUARD_S = 0.05
    _STATUS_TAIL_S = 0.15
    _CONTROL_POLL_S = 0.02
    _STATUS_STALE_S = 0.6
    _CLOSED_INTENT_WITHOUT_SEQUENCE = object()

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
        self._audio_lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._control_thread: threading.Thread | None = None
        self._process: subprocess.Popen | None = None
        self._controller = None
        self._token: str | None = None
        self._enabled = False
        self._transmitting = False
        self._audio_accepting = False
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
        self._slot_deadline: float | None = None
        self._closed_intent_sequence: int | object | None = None
        self._enable_generation = 0
        self._ptt_unconfirmed = False
        self._radio_state: dict = {}
        self._radio_seen_at = 0.0
        self._status_polled_at = 0.0
        self._status_generation = 0
        self._status_enable_generation = 0
        self._status_poll_active_generation = 0
        self._status_poll_sequence = 0
        self._status_poll_active_sequence = 0
        self._onset_generation = 0
        self._onset_poll_sequence = 0
        self._pending_fault: WsjtxTxAudioError | None = None
        self._fault_sequence = 0
        self._fault_category = ""
        self._status_poll_max_ms = 0.0
        self._control_operation_max_ms = 0.0
        self._pipe_queue_max_ms = 0.0
        self._onset_max_ms = 0.0
        self._read_max_ms = 0.0
        self._send_max_ms = 0.0

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            status_age_ms = (
                max(0.0, (time.monotonic() - self._status_polled_at) * 1000)
                if self._status_polled_at else None
            )
            return {"state": self._state, "detail": self._detail, "enabled": self._enabled,
                    "transmitting": self._transmitting, "message": self._message,
                    "sample_rate": self._rate, "frames": self._frames, "sequence": self._sequence,
                    "capture_running": self._process is not None and self._process.poll() is None,
                    "owns_transmit": self._token is not None,
                    "ptt_unconfirmed": self._ptt_unconfirmed,
                    "fault_sequence": self._fault_sequence,
                    "fault_category": self._fault_category,
                    "diagnostics": {
                        "status_age_ms": round(status_age_ms, 3) if status_age_ms is not None else None,
                        "status_generation": self._status_generation,
                        "status_enable_generation": self._status_enable_generation,
                        "status_poll_active_generation": self._status_poll_active_generation,
                        "status_poll_active_sequence": self._status_poll_active_sequence,
                        "status_poll_max_ms": round(self._status_poll_max_ms, 3),
                        "control_operation_max_ms": round(self._control_operation_max_ms, 3),
                        "pipe_queue_max_ms": round(self._pipe_queue_max_ms, 3),
                        "onset_max_ms": round(self._onset_max_ms, 3),
                        "read_max_ms": round(self._read_max_ms, 3),
                        "send_max_ms": round(self._send_max_ms, 3),
                    }}

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
            raise WsjtxTxAudioError(reason, category="control")
        if controller is not self._get_controller() or not self._radio(controller).get("connected"):
            raise WsjtxTxAudioError(
                "Native radio disconnected or changed; TX stopped.", category="control"
            )
        if int(controller.config.sample_rate) != self._rate:
            raise WsjtxTxAudioError(
                "Native radio audio format changed; TX stopped.", category="format"
            )
        if controller.config.tx_codec not in ("lpcm16_mono", "lpcm16_stereo"):
            raise WsjtxTxAudioError(
                "Native radio transmit audio requires 16-bit LPCM.", category="format"
            )

    @staticmethod
    def _classified(error: Exception, category: str) -> WsjtxTxAudioError:
        if isinstance(error, WsjtxTxAudioError):
            return error
        message = str(error) or type(error).__name__
        return WsjtxTxAudioError(message, category=category)

    def _request_fault(self, error: Exception, *, category: str = "fatal") -> None:
        fault = self._classified(error, category)
        with self._lock:
            if self._pending_fault is None:
                self._pending_fault = fault
                self._enable_generation += 1
                self._enabled = False
                self._audio_accepting = False
                self._onset_generation = 0
                self._onset_poll_sequence = 0
        self._stop.set()

    @staticmethod
    def _observe_max(current: float, started_at: float) -> float:
        return max(current, (time.perf_counter() - started_at) * 1000)

    def _intent_is_current(self, generation: int) -> bool:
        with self._lock:
            return (
                not self._stop.is_set()
                and self._enabled
                and generation == self._enable_generation
            )

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
            self._ptt_unconfirmed = False
            self._slot_deadline = None
            self._closed_intent_sequence = None
            self._state, self._detail = "listening", "Managed TX output is isolated; transmitter is disabled."
            self._status_polled_at = 0.0
            self._status_generation = 0
            self._status_enable_generation = 0
            self._status_poll_active_generation = 0
            self._status_poll_sequence = 0
            self._status_poll_active_sequence = 0
            self._onset_generation = 0
            self._onset_poll_sequence = 0
            self._audio_accepting = False
            self._pending_fault = None
            self._fault_category = ""
            self._status_poll_max_ms = 0.0
            self._control_operation_max_ms = 0.0
            self._pipe_queue_max_ms = 0.0
            self._onset_max_ms = 0.0
            self._read_max_ms = 0.0
            self._send_max_ms = 0.0
            self._control_thread = threading.Thread(
                target=self._control_worker, name="wsjtx-tx-control", daemon=True
            )
            self._thread = threading.Thread(target=self._run, name="wsjtx-tx-audio", daemon=True)
            self._control_thread.start()
            self._thread.start()

    def set_enabled(self, enabled: bool) -> None:
        if not enabled:
            self._request_disarm()
            with self._operation:
                self._disable()
            return
        with self._lock:
            generation = self._enable_generation
        with self._operation:
            with self._lock:
                if generation != self._enable_generation:
                    raise RuntimeError("Managed transmit enable was cancelled.")
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
                with self._lock:
                    cancelled = generation != self._enable_generation
                if cancelled:
                    raise RuntimeError("Managed transmit enable was cancelled.")
                state = controller.configure_microphone(use_transport=True) or {}
                microphone = state.get("microphone", {})
                if microphone.get("lan_mod_level", 0) <= 0:
                    raise RuntimeError("Radio LAN modulation level is zero or unavailable.")
                # The controller verifies the transport input when configuring it.
                self._check(controller)
                with self._lock:
                    if generation != self._enable_generation:
                        raise RuntimeError("Managed transmit enable was cancelled.")
                    self._controller, self._token = controller, token
                    self._enabled = True
                    self._state, self._detail = "armed", "Waiting for the selected WSJT-X transmit slot."
            except Exception:
                try:
                    controller.release_transmit_stream(token)
                except Exception:
                    if self._radio(controller).get("connected"):
                        raise
                raise

    def _request_disarm(self) -> None:
        with self._lock:
            self._enable_generation += 1
            self._enabled = False
            self._onset_generation = 0
            self._onset_poll_sequence = 0
            self._tail_until = None

    def _release_key(self) -> str | None:
        with self._lock:
            controller, token = self._controller, self._token
        if controller is None or token is None:
            return None
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
                        self._audio_accepting = False
                        self._tail_until = None
                        self._slot_deadline = None
                        self._sequence += 1
                    self._ptt_unconfirmed = False
                    return None
            except Exception as error:
                problem = str(error)
            if not self._radio(controller).get("connected"):
                return problem
        raise RuntimeError(f"WSJT-X could not confirm PTT OFF: {problem}")

    def _disable(self) -> None:
        with self._lock:
            self._enabled = False
            self._audio_accepting = False
            controller, token = self._controller, self._token
            keyed = self._transmitting
        release_problem = ""
        uncertainty_preserved = False
        with self._audio_lock:
            if controller is not None and token is not None:
                controller_uncertain = self._radio(controller).get("tx_unconfirmed") is True
                try:
                    if keyed or controller.transmit_active or controller_uncertain:
                        release_problem = self._release_key() or ""
                    if not release_problem:
                        try:
                            controller.release_transmit_stream(token)
                        except Exception as error:
                            if self._radio(controller).get("connected"):
                                raise
                            release_problem = str(error)
                except Exception as error:
                    if self._radio(controller).get("connected"):
                        raise
                    release_problem = str(error)
                uncertainty_preserved = self._radio(controller).get("tx_unconfirmed") is True
        with self._lock:
            self._controller = self._token = None
            self._transmitting = False
            self._audio_accepting = False
            self._tail_until = None
            self._slot_deadline = None
            self._ptt_unconfirmed = bool(release_problem or uncertainty_preserved)
            if self._ptt_unconfirmed:
                warning = (
                    "WSJT-X could not confirm PTT OFF because the radio disconnected: "
                    f"{release_problem or 'the controller reports an unconfirmed PTT state.'}"
                )
                self._detail = "; ".join(filter(None, (self._detail, warning)))
            if self._state != "error":
                self._state = "listening"
                if not self._ptt_unconfirmed:
                    self._detail = "Transmitter is disabled."

    @classmethod
    def _utc_slot(cls, mode: str) -> tuple[bool, float]:
        period = cls._SLOT_PERIODS_S.get(mode.upper())
        if period is None:
            raise RuntimeError(f"WSJT-X reported unsupported TX mode {mode!r}; PTT was not keyed.")
        now = time.time()
        slot_index = int(now // period)
        return slot_index % 2 == 0, (slot_index + 1) * period

    def _finish_transmission(
        self,
        controller: Any,
        token: str | None,
        *,
        status_sequence: int | None = None,
        intent_still_active: bool = False,
    ) -> None:
        with self._lock:
            self._audio_accepting = False
        with self._audio_lock:
            controller.flush_audio(transmit_token=token)
            release_problem = self._release_key()
        if release_problem is not None:
            raise RuntimeError(f"WSJT-X could not confirm PTT OFF: {release_problem}")
        with self._lock:
            if intent_still_active:
                self._closed_intent_sequence = (
                    status_sequence
                    if type(status_sequence) is int
                    else self._CLOSED_INTENT_WITHOUT_SEQUENCE
                )
            else:
                self._closed_intent_sequence = None
            self._state, self._detail = "armed", "Waiting for the next transmit slot."

    def _update_intent(self, status: dict, now: float) -> None:
        with self._lock:
            enabled, keyed = self._enabled, self._transmitting
            controller, token = self._controller, self._token
            slot_deadline = self._slot_deadline
            generation = self._enable_generation
        if not enabled:
            return
        self._check(controller)
        if not self._intent_is_current(generation):
            return
        if not status.get("connected"):
            raise WsjtxTxAudioError(
                "Managed WSJT-X status is unavailable; transmitter stopped.",
                category="control",
            )
        intent = status.get("transmitting") is True
        status_sequence = status.get("status_sequence")
        with self._lock:
            closed_sequence = self._closed_intent_sequence
            if not intent:
                self._closed_intent_sequence = None
            elif closed_sequence is not None:
                if (
                    closed_sequence is self._CLOSED_INTENT_WITHOUT_SEQUENCE
                    or type(status_sequence) is not int
                    or status_sequence <= closed_sequence
                ):
                    return
                self._closed_intent_sequence = None
        if intent and not keyed:
            tx_even = status.get("tx_even")
            if type(tx_even) is not bool:
                raise WsjtxTxAudioError(
                    "WSJT-X did not provide verified transmit-slot parity; PTT was not keyed.",
                    category="control",
                )
            _, slot_end = self._utc_slot(str(status.get("mode", "")))
            if slot_end - time.time() <= self._SLOT_RELEASE_GUARD_S:
                return
            if self._prepared_message and str(status.get("tx_message", "")).strip() != self._prepared_message:
                self._unmatched_since = self._unmatched_since or now
                if self._request_status is not None and now - self._status_requested_at >= 0.3:
                    self._request_status()
                    self._status_requested_at = now
                if now - self._unmatched_since > 0.4:
                    raise WsjtxTxAudioError(
                        f"Stock transmit message {status.get('tx_message', '')!r} does not match {self._prepared_message!r}; PTT was not keyed.",
                        category="control",
                    )
                return
            self._unmatched_since = None
            if not self._intent_is_current(generation):
                return
            controller.set_transmit_stream_paced(True, transmit_token=token)
            if not self._intent_is_current(generation):
                return
            controller.set_ptt(True, transmit_token=token)
            keyed_at = time.monotonic()
            remaining = max(0.0, slot_end - time.time() - self._SLOT_RELEASE_GUARD_S)
            late = remaining <= 0
            with self._lock:
                cancelled = (
                    late
                    or
                    self._stop.is_set()
                    or not self._enabled
                    or generation != self._enable_generation
                )
                if cancelled:
                    self._transmitting = True
                    self._audio_accepting = False
                    self._keyed_at = keyed_at
                    self._slot_deadline = keyed_at + remaining
                else:
                    self._transmitting = True
                    self._audio_accepting = True
                    self._keyed_at = keyed_at
                    self._slot_deadline = keyed_at + remaining
                    self._tail_until = None
                    self._closed_intent_sequence = None
                    self._message = str(status.get("tx_message", ""))
                    self._sequence += 1
                    self._state, self._detail = "transmitting", self._message
            if cancelled:
                controller.flush_audio(transmit_token=token)
                release_problem = self._release_key()
                if release_problem is not None:
                    raise RuntimeError(f"WSJT-X could not confirm PTT OFF: {release_problem}")
                if late and self._intent_is_current(generation):
                    raise WsjtxTxAudioError(
                        "PTT confirmation crossed the transmit slot; audio was not sent.",
                        category="control",
                    )
                return
            LOGGER.info("WSJT-X native transmit started: %s", self._message)
        elif keyed:
            if intent:
                self._tail_until = None
            maximum = 8.5 if status.get("mode") == "FT4" else 16.0
            if now - self._keyed_at > maximum:
                raise WsjtxTxAudioError(
                    "WSJT-X transmit exceeded its slot; transmitter stopped.",
                    category="control",
                )
            if not intent and self._tail_until is None:
                self._tail_until = min(now + self._STATUS_TAIL_S, slot_deadline or float("inf"))
            if self._tail_until is not None and now >= self._tail_until:
                self._finish_transmission(controller, token)

    def _control_worker(self) -> None:
        while not self._stop.is_set():
            with self._lock:
                enable_generation = self._enable_generation
                self._status_poll_sequence += 1
                poll_sequence = self._status_poll_sequence
                self._status_poll_active_generation = enable_generation
                self._status_poll_active_sequence = poll_sequence
            poll_started = time.perf_counter()
            try:
                status = self._get_status()
            except Exception as error:
                with self._lock:
                    if self._status_poll_active_sequence == poll_sequence:
                        self._status_poll_active_generation = 0
                        self._status_poll_active_sequence = 0
                self._request_fault(error, category="control")
                return
            if not isinstance(status, dict):
                with self._lock:
                    if self._status_poll_active_sequence == poll_sequence:
                        self._status_poll_active_generation = 0
                        self._status_poll_active_sequence = 0
                self._request_fault(
                    ValueError("Managed WSJT-X status is not an object."),
                    category="control",
                )
                return
            polled_at = time.monotonic()
            with self._lock:
                if self._stop.is_set() or enable_generation != self._enable_generation:
                    if self._status_poll_active_sequence == poll_sequence:
                        self._status_poll_active_generation = 0
                        self._status_poll_active_sequence = 0
                    continue
                self._status_polled_at = polled_at
                self._status_generation += 1
                self._status_enable_generation = enable_generation
                self._status_poll_max_ms = self._observe_max(
                    self._status_poll_max_ms, poll_started
                )
                enabled = self._enabled
                intent = status.get("transmitting") is True
                prepared = self._prepared_message
                status_message = str(status.get("tx_message", "")).strip()
                status_sequence = status.get("status_sequence")
                closed_sequence = self._closed_intent_sequence
                closed_current = (
                    closed_sequence is None
                    or (
                        type(closed_sequence) is int
                        and type(status_sequence) is int
                        and status_sequence > closed_sequence
                    )
                )
                onset_current = (
                    enabled
                    and intent
                    and not self._transmitting
                    and (not prepared or status_message == prepared)
                    and closed_current
                )
                if onset_current:
                    self._onset_generation = enable_generation
                    self._onset_poll_sequence = poll_sequence
                elif not self._transmitting:
                    self._onset_generation = 0
                    self._onset_poll_sequence = 0
                self._status_poll_active_generation = 0
                self._status_poll_active_sequence = 0
            if self._stop.is_set():
                return
            if enabled:
                operation_started = time.perf_counter()
                try:
                    with self._operation:
                        if self._stop.is_set():
                            return
                        self._update_intent(status, polled_at)
                except Exception as error:
                    self._request_fault(error, category="control")
                    return
                with self._lock:
                    self._control_operation_max_ms = self._observe_max(
                        self._control_operation_max_ms, operation_started
                    )
                    if not self._transmitting:
                        self._onset_generation = 0
                        self._onset_poll_sequence = 0
            remaining = self._CONTROL_POLL_S - (time.monotonic() - polled_at)
            if remaining > 0 and self._stop.wait(remaining):
                return

    def _run(self) -> None:
        import fcntl
        import termios

        process = self._process
        assert process is not None and process.stdout is not None
        descriptor = process.stdout.fileno()
        partial = bytearray()
        onset = bytearray()
        onset_generation = 0
        onset_poll_sequence = 0
        read_size = max(2, self._rate // 50 * 2)
        last_pcm = time.monotonic()
        try:
            while not self._stop.is_set():
                with self._lock:
                    pending_fault = self._pending_fault
                    transmitting = self._transmitting
                    enabled = self._enabled
                    status_polled_at = self._status_polled_at
                if pending_fault is not None:
                    raise pending_fault
                now = time.monotonic()
                if enabled and status_polled_at and now - status_polled_at > self._STATUS_STALE_S:
                    raise WsjtxTxAudioError(
                        "Managed WSJT-X control status is stale; transmitter stopped.",
                        category="control",
                    )
                if transmitting and now - max(last_pcm, self._keyed_at) > 0.6:
                    raise WsjtxTxAudioError(
                        "Managed transmit audio stalled; transmitter stopped.",
                        category="capture_stall",
                    )
                if process.poll() is not None:
                    raise WsjtxTxAudioError(
                        "Managed transmit audio capture exited.", category="capture_exit"
                    )
                ready, _, _ = select.select([descriptor], [], [], 0.02)
                if not ready:
                    continue
                queued = array.array("i", [0])
                fcntl.ioctl(descriptor, termios.FIONREAD, queued, True)
                with self._lock:
                    transmitting = self._transmitting
                    generation = self._enable_generation
                    pending_generation = self._onset_generation
                    pending_poll_sequence = self._onset_poll_sequence
                    poll_generation = self._status_poll_active_generation
                    poll_sequence = self._status_poll_active_sequence
                    self._pipe_queue_max_ms = max(
                        self._pipe_queue_max_ms,
                        queued[0] / max(1, self._rate * 2) * 1000,
                    )
                capture_active = (
                    transmitting
                    or bool(onset)
                    or (pending_generation == generation and pending_poll_sequence)
                    or (poll_generation == generation and poll_sequence)
                )
                if capture_active and queued[0] + len(onset) > self._rate * 2 * 0.4:
                    raise WsjtxTxAudioError(
                        "Managed transmit audio backlog exceeded 400 ms; transmitter stopped.",
                        category="capture_backlog",
                    )
                read_started = time.perf_counter()
                try:
                    chunk = os.read(descriptor, read_size)
                except OSError as error:
                    raise self._classified(error, "capture_closed") from error
                with self._lock:
                    self._read_max_ms = self._observe_max(self._read_max_ms, read_started)
                if not chunk:
                    raise WsjtxTxAudioError(
                        "Managed transmit audio capture closed.", category="capture_closed"
                    )
                last_pcm = time.monotonic()
                partial.extend(chunk)
                count = len(partial) // 2 * 2
                pcm = bytes(partial[:count])
                del partial[:count]
                with self._lock:
                    enabled = self._enabled
                    transmitting = self._transmitting
                    generation = self._enable_generation
                    pending_generation = self._onset_generation
                    pending_poll_sequence = self._onset_poll_sequence
                    poll_generation = self._status_poll_active_generation
                    poll_sequence = self._status_poll_active_sequence
                    accepting = self._audio_accepting
                    controller, token = self._controller, self._token
                if onset_generation != generation:
                    onset.clear()
                    onset_generation = 0
                    onset_poll_sequence = 0
                candidate_sequence = (
                    pending_poll_sequence
                    if pending_generation == generation and pending_poll_sequence
                    else poll_sequence
                    if poll_generation == generation and poll_sequence
                    else 0
                )
                if candidate_sequence != onset_poll_sequence:
                    onset.clear()
                    onset_poll_sequence = candidate_sequence
                if enabled and not transmitting and candidate_sequence:
                    onset.extend(pcm)
                    onset_generation = generation
                    self._onset_max_ms = max(
                        self._onset_max_ms,
                        len(onset) / max(1, self._rate * 2) * 1000,
                    )
                    if len(onset) > self._rate * 2 * 0.4:
                        raise WsjtxTxAudioError(
                            "Managed transmit onset exceeded 400 ms; transmitter stopped.",
                            category="capture_backlog",
                        )
                    continue
                if not enabled or not transmitting:
                    onset.clear()
                    onset_generation = 0
                    onset_poll_sequence = 0
                    continue
                if (onset
                        and onset_generation == generation
                        and onset_poll_sequence == pending_poll_sequence):
                    if len(onset) + len(pcm) > self._rate * 2 * 0.4:
                        raise WsjtxTxAudioError(
                            "Managed transmit onset exceeded 400 ms; transmitter stopped.",
                            category="capture_backlog",
                        )
                    pcm = bytes(onset) + pcm
                onset.clear()
                onset_generation = 0
                onset_poll_sequence = 0
                should_send = bool(pcm and accepting)
                if should_send:
                    send_started = time.perf_counter()
                    try:
                        with self._audio_lock:
                            with self._lock:
                                deadline = self._slot_deadline
                                current = (
                                    self._enabled
                                    and self._transmitting
                                    and self._audio_accepting
                                    and self._token == token
                                )
                            if current and deadline is not None and time.monotonic() >= deadline:
                                raise WsjtxTxAudioError(
                                    "Managed transmit slot ended; delayed audio was not sent.",
                                    category="control",
                                )
                            if current:
                                controller.send_audio(pcm, transmit_token=token)
                            else:
                                continue
                    except Exception as error:
                        with self._lock:
                            current = (
                                self._enabled and self._transmitting and self._token == token
                            )
                        if current:
                            raise self._classified(error, "transport") from error
                        continue
                    with self._lock:
                        self._frames += len(pcm) // 2
                        self._send_max_ms = self._observe_max(self._send_max_ms, send_started)
        except Exception as error:
            self._request_fault(error)
        finally:
            cleanup_fault: WsjtxTxAudioError | None = None
            with self._operation:
                try:
                    self._disable()
                except Exception as error:
                    cleanup_fault = self._classified(error, "control")
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=1)
            with self._lock:
                fault = cleanup_fault or self._pending_fault
                if fault is not None:
                    self._fault_sequence += 1
                    self._fault_category = fault.category
                    self._state, self._detail = "error", str(fault)
                    LOGGER.error("WSJT-X transmit stopped (%s): %s", fault.category, fault)

    def stop(self) -> dict[str, Any]:
        self._request_disarm()
        self._stop.set()
        deadline = time.monotonic() + 4
        for thread in (self._thread, self._control_thread):
            if thread is not None and thread is not threading.current_thread():
                thread.join(timeout=max(0.0, deadline - time.monotonic()))
                if thread.is_alive():
                    raise RuntimeError(
                        "Managed transmit shutdown did not finish; check radio PTT."
                    )
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
            self._thread = self._control_thread = self._process = None
            if cleanup_error is not None:
                self._state = "error"
                self._detail = "; ".join(filter(None, (self._detail, str(cleanup_error))))
            elif self._state == "error" or self._ptt_unconfirmed:
                # Resources are released now. Keep the reason the capture ended,
                # or the bounded warning from the disconnected release attempt,
                # but this is a completed cleanup rather than a live failure.
                self._state = "stopped"
            else:
                self._state, self._detail = "stopped", "Managed transmit audio capture is stopped."
            return self.snapshot()
