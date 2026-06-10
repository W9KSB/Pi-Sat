from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from threading import Event, Lock, Thread
from typing import Any

from pi_sat_controller.backend.radio.rigctld_client import PersistentRigctldClient
from pi_sat_controller.backend.radio.radio_manager import RadioManager


@dataclass(frozen=True)
class SdrDeviceSnapshot:
    enabled: bool
    connected: bool
    frequency_hz: int | None
    last_read_at_utc: str | None
    last_write_at_utc: str | None
    error: str | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "connected": self.connected,
            "frequency_hz": self.frequency_hz,
            "last_read_at_utc": self.last_read_at_utc,
            "last_write_at_utc": self.last_write_at_utc,
            "error": self.error,
        }


class PollingSdrManager:
    def __init__(
        self,
        host: str,
        port: int,
        timeout_s: float,
        poll_interval_s: float = 1.0,
    ) -> None:
        self.client = PersistentRigctldClient(host, port, timeout_s)
        self.poll_interval_s = poll_interval_s
        self._stop = Event()
        self._state_lock = Lock()
        self._client_lock = Lock()
        self._thread: Thread | None = None
        self._connected = False
        self._frequency_hz: int | None = None
        self._last_read_at_utc: str | None = None
        self._last_write_at_utc: str | None = None
        self._error: str | None = None

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = Thread(target=self._run, name="sdr-rigctl-poller", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None
        with self._client_lock:
            self.client.close()

    def snapshot(self) -> SdrDeviceSnapshot:
        with self._state_lock:
            return SdrDeviceSnapshot(
                enabled=True,
                connected=self._connected,
                frequency_hz=self._frequency_hz,
                last_read_at_utc=self._last_read_at_utc,
                last_write_at_utc=self._last_write_at_utc,
                error=self._error,
            )

    def set_frequency(self, frequency_hz: int) -> SdrDeviceSnapshot:
        if frequency_hz <= 0:
            raise ValueError("frequency_hz must be a positive integer")

        with self._client_lock:
            try:
                self.client.set_frequency(frequency_hz)
            except Exception as exc:
                self._record_error(exc)
                raise

        now = _utc_now()
        with self._state_lock:
            self._connected = True
            self._frequency_hz = frequency_hz
            self._last_write_at_utc = now
            self._error = None
        return self.snapshot()

    def _run(self) -> None:
        while not self._stop.is_set():
            self.poll_once()
            self._stop.wait(self.poll_interval_s)

    def poll_once(self) -> None:
        with self._client_lock:
            try:
                frequency_hz = self.client.get_frequency()
            except Exception as exc:
                self._record_error(exc)
                return

        with self._state_lock:
            self._connected = True
            self._frequency_hz = frequency_hz
            self._last_read_at_utc = _utc_now()
            self._error = None

    def _record_error(self, exc: Exception) -> None:
        with self._state_lock:
            self._connected = False
            self._error = str(exc)


class PollingRadioFrequencyManager:
    def __init__(
        self,
        radio_manager: RadioManager,
        poll_interval_s: float = 1.0,
    ) -> None:
        self.radio_manager = radio_manager
        self.poll_interval_s = poll_interval_s
        self._stop = Event()
        self._thread: Thread | None = None

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = Thread(target=self._run, name="rx-radio-poller", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None
        if hasattr(self.radio_manager.client, "close"):
            self.radio_manager.client.close()

    def snapshot(self) -> SdrDeviceSnapshot:
        state = self.radio_manager.snapshot()
        return SdrDeviceSnapshot(
            enabled=state.enabled,
            connected=state.connected,
            frequency_hz=state.frequency_hz,
            last_read_at_utc=state.last_read_at_utc,
            last_write_at_utc=state.last_write_at_utc,
            error=state.error,
        )

    def set_frequency(self, frequency_hz: int) -> SdrDeviceSnapshot:
        state = self.radio_manager.set_frequency(
            frequency_hz,
            source="rx_tracking",
        )
        return SdrDeviceSnapshot(
            enabled=state.enabled,
            connected=state.connected,
            frequency_hz=state.frequency_hz,
            last_read_at_utc=state.last_read_at_utc,
            last_write_at_utc=state.last_write_at_utc,
            error=state.error,
        )

    def set_mode(self, mode: str, source: str = "") -> SdrDeviceSnapshot:
        state = self.radio_manager.set_mode(mode, source=source)
        return SdrDeviceSnapshot(
            enabled=state.enabled,
            connected=state.connected,
            frequency_hz=state.frequency_hz,
            last_read_at_utc=state.last_read_at_utc,
            last_write_at_utc=state.last_write_at_utc,
            error=state.error,
        )

    def set_vfo(self, vfo: str | None, source: str = "") -> SdrDeviceSnapshot:
        state = self.radio_manager.set_vfo(vfo, source=source)
        return SdrDeviceSnapshot(
            enabled=state.enabled,
            connected=state.connected,
            frequency_hz=state.frequency_hz,
            last_read_at_utc=state.last_read_at_utc,
            last_write_at_utc=state.last_write_at_utc,
            error=state.error,
        )

    def _run(self) -> None:
        while not self._stop.is_set():
            self.poll_once()
            self._stop.wait(self.poll_interval_s)

    def poll_once(self) -> SdrDeviceSnapshot:
        state = self.radio_manager.poll_once()
        return SdrDeviceSnapshot(
            enabled=state.enabled,
            connected=state.connected,
            frequency_hz=state.frequency_hz,
            last_read_at_utc=state.last_read_at_utc,
            last_write_at_utc=state.last_write_at_utc,
            error=state.error,
        )


def disabled_sdr_snapshot() -> SdrDeviceSnapshot:
    return SdrDeviceSnapshot(
        enabled=False,
        connected=False,
        frequency_hz=None,
        last_read_at_utc=None,
        last_write_at_utc=None,
        error="SDR is disabled in pi-sat-controller.conf",
    )


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()
