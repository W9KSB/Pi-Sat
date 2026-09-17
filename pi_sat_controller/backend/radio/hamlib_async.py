from __future__ import annotations

from dataclasses import dataclass
import json
import logging
import re
import socket
import subprocess
from threading import Event, Lock, Thread
from time import monotonic
from typing import Any, Callable

from pi_sat_controller.backend.radio.radio_state import (
    AsyncCapabilityState,
    RadioStateClassification,
    RadioStateEvent,
    RadioStateProperty,
    normalize_event_vfo,
)


LOGGER = logging.getLogger(__name__)
MINIMUM_HAMLIB_ASYNC_VERSION = (4, 6, 0)
MAX_ASYNC_DATAGRAM_BYTES = 1024 * 1024


@dataclass(frozen=True)
class HamlibAsyncCapability:
    state: AsyncCapabilityState
    hamlib_version: str | None
    backend_supported: bool
    reason: str

    def to_dict(self) -> dict[str, object]:
        return {
            "state": self.state.value,
            "hamlib_version": self.hamlib_version,
            "backend_supported": self.backend_supported,
            "reason": self.reason,
        }


def probe_hamlib_async_capability(
    model_id: int,
    timeout_s: float = 5.0,
) -> HamlibAsyncCapability:
    """Probe the installed rigctld and selected backend without opening a radio."""

    timeout = min(max(float(timeout_s), 1.0), 5.0)
    try:
        version_result = subprocess.run(
            ["rigctld", "--version"],
            capture_output=True,
            check=False,
            text=True,
            timeout=timeout,
        )
    except Exception as exc:
        return HamlibAsyncCapability(
            state=AsyncCapabilityState.UNSUPPORTED,
            hamlib_version=None,
            backend_supported=False,
            reason=f"Hamlib version probe failed: {exc}",
        )

    version_text = (version_result.stdout or version_result.stderr or "").strip()
    parsed_version = parse_hamlib_version(version_text)
    if version_result.returncode != 0 or parsed_version is None:
        return HamlibAsyncCapability(
            state=AsyncCapabilityState.UNSUPPORTED,
            hamlib_version=version_text or None,
            backend_supported=False,
            reason="Installed Hamlib version could not be identified.",
        )
    if parsed_version < MINIMUM_HAMLIB_ASYNC_VERSION:
        return HamlibAsyncCapability(
            state=AsyncCapabilityState.UNSUPPORTED,
            hamlib_version=version_text,
            backend_supported=False,
            reason=(
                "Hamlib 4.6 or newer is required for the generic async state "
                "publisher configuration used by Pi-Sat."
            ),
        )

    try:
        caps_result = subprocess.run(
            ["rigctld", "-m", str(int(model_id)), "-u"],
            capture_output=True,
            check=False,
            text=True,
            timeout=timeout,
        )
    except Exception as exc:
        return HamlibAsyncCapability(
            state=AsyncCapabilityState.UNSUPPORTED,
            hamlib_version=version_text,
            backend_supported=False,
            reason=f"Hamlib backend capability probe failed: {exc}",
        )

    caps_output = "\n".join(
        value for value in (caps_result.stdout, caps_result.stderr) if value
    )
    supported = bool(
        re.search(r"^\s*Has async data support:\s*(?:Y|Yes|True|1)\s*$", caps_output, re.I | re.M)
    )
    if caps_result.returncode != 0 or not supported:
        return HamlibAsyncCapability(
            state=AsyncCapabilityState.UNSUPPORTED,
            hamlib_version=version_text,
            backend_supported=False,
            reason="The selected Hamlib backend does not advertise async data support.",
        )
    return HamlibAsyncCapability(
        state=AsyncCapabilityState.AVAILABLE,
        hamlib_version=version_text,
        backend_supported=True,
        reason="The selected Hamlib backend advertises generic async data support.",
    )


def parse_hamlib_version(value: str) -> tuple[int, int, int] | None:
    match = re.search(r"(?<!\d)(\d+)\.(\d+)(?:\.(\d+))?", value)
    if match is None:
        return None
    return (
        int(match.group(1)),
        int(match.group(2)),
        int(match.group(3) or 0),
    )


class HamlibAsyncStateListener:
    """Receives Hamlib JSON snapshots on a socket separate from rigctld TCP."""

    def __init__(
        self,
        event_callback: Callable[[RadioStateEvent], None],
        bind_host: str = "127.0.0.1",
    ) -> None:
        self.event_callback = event_callback
        self.bind_host = bind_host
        self._lock = Lock()
        self._stop = Event()
        self._socket: socket.socket | None = None
        self._thread: Thread | None = None
        self._port: int | None = None
        self._running = False
        self._last_packet_at: float | None = None
        self._last_error: str | None = None
        self._previous_values: dict[tuple[str | None, RadioStateProperty], Any] = {}
        self._last_sequence: int | None = None

    @property
    def port(self) -> int:
        with self._lock:
            if self._port is None:
                raise RuntimeError("Hamlib async listener is not started")
            return self._port

    @property
    def running(self) -> bool:
        with self._lock:
            thread_alive = self._thread is not None and self._thread.is_alive()
            return self._running and thread_alive and self._last_error is None

    @property
    def last_packet_at(self) -> float | None:
        with self._lock:
            return self._last_packet_at

    @property
    def last_error(self) -> str | None:
        with self._lock:
            return self._last_error

    def start(self) -> int:
        with self._lock:
            if self._thread is not None and self._thread.is_alive() and self._port is not None:
                return self._port
            udp_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            udp_socket.bind((self.bind_host, 0))
            udp_socket.settimeout(0.5)
            self._socket = udp_socket
            self._port = int(udp_socket.getsockname()[1])
            self._stop.clear()
            self._last_error = None
            self._running = True
            self._thread = Thread(
                target=self._run,
                name=f"hamlib-async-{self._port}",
                daemon=True,
            )
            self._thread.start()
            return self._port

    def stop(self) -> None:
        self._stop.set()
        with self._lock:
            udp_socket = self._socket
            thread = self._thread
            self._running = False
        if udp_socket is not None:
            try:
                udp_socket.close()
            except OSError:
                pass
        if thread is not None:
            thread.join(timeout=1.0)
        with self._lock:
            self._socket = None
            self._thread = None
            self._port = None
            self._previous_values.clear()
            self._last_sequence = None

    def seed_observed(
        self,
        property: RadioStateProperty,
        value: Any,
        vfo: str | None,
    ) -> None:
        key = (normalize_event_vfo(vfo), property)
        with self._lock:
            self._previous_values[key] = value

    def status(self) -> dict[str, object]:
        return {
            "listener_running": self.running,
            "last_async_event_monotonic": self.last_packet_at,
            "listener_error": self.last_error,
        }

    def _run(self) -> None:
        while not self._stop.is_set():
            with self._lock:
                udp_socket = self._socket
            if udp_socket is None:
                break
            try:
                payload, _address = udp_socket.recvfrom(MAX_ASYNC_DATAGRAM_BYTES)
            except socket.timeout:
                continue
            except OSError as exc:
                if self._stop.is_set():
                    break
                self._fail(exc)
                break
            try:
                events, sequence = self._parse_snapshot(payload)
            except Exception as exc:
                LOGGER.debug("Ignored invalid Hamlib async snapshot: %s", exc)
                continue
            received_at = monotonic()
            with self._lock:
                if (
                    sequence is not None
                    and self._last_sequence is not None
                    and sequence > self._last_sequence + 1
                ):
                    LOGGER.debug(
                        "Hamlib async sequence gap previous=%s current=%s",
                        self._last_sequence,
                        sequence,
                    )
                self._last_sequence = sequence
                self._last_packet_at = received_at
            for event in events:
                try:
                    self.event_callback(event)
                except Exception as exc:
                    self._fail(exc)
                    return
        with self._lock:
            self._running = False

    def _parse_snapshot(
        self,
        payload: bytes,
    ) -> tuple[list[RadioStateEvent], int | None]:
        document = json.loads(payload.decode("utf-8"))
        if not isinstance(document, dict):
            raise ValueError("snapshot root is not an object")
        if str(document.get("app", "")).strip().lower() != "hamlib":
            raise ValueError("snapshot was not published by Hamlib")
        vfos = document.get("vfos")
        if not isinstance(vfos, list):
            raise ValueError("snapshot has no VFO array")

        timestamp = monotonic()
        events: list[RadioStateEvent] = []
        for raw_vfo in vfos:
            if not isinstance(raw_vfo, dict):
                continue
            vfo = normalize_event_vfo(str(raw_vfo.get("name", "")))
            raw_rx = raw_vfo.get("rx")
            raw_tx = raw_vfo.get("tx")
            role = None
            if raw_rx is True and raw_tx is not True:
                role = "rx"
            elif raw_tx is True and raw_rx is not True:
                role = "tx"
            values: list[tuple[RadioStateProperty, Any]] = []
            try:
                frequency_hz = int(raw_vfo.get("freq", 0))
            except (TypeError, ValueError):
                frequency_hz = 0
            if frequency_hz > 0:
                values.append((RadioStateProperty.FREQUENCY, frequency_hz))
            mode = str(raw_vfo.get("mode", "")).strip().upper()
            if mode and mode not in {"NONE", "UNKNOWN"}:
                values.append((RadioStateProperty.MODE, mode))

            for property, value in values:
                key = (vfo, property)
                with self._lock:
                    previous = self._previous_values.get(key)
                    self._previous_values[key] = value
                classification = (
                    RadioStateClassification.STATE_REFRESH
                    if previous is None or previous == value
                    else RadioStateClassification.EXTERNAL_CHANGE
                )
                events.append(
                    RadioStateEvent(
                        property=property,
                        value=value,
                        role=role,
                        vfo=vfo,
                        timestamp=timestamp,
                        source="hamlib_udp_snapshot",
                        classification=classification,
                    )
                )
        sequence = document.get("seq")
        return events, int(sequence) if isinstance(sequence, (int, float)) else None

    def _fail(self, exc: Exception) -> None:
        with self._lock:
            self._last_error = str(exc)
            self._running = False
        LOGGER.warning("Hamlib async listener failed: %s", exc)
