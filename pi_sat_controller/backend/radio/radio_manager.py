from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import logging
from threading import RLock
from typing import Any, Protocol

LOGGER = logging.getLogger(__name__)
DEFAULT_FAILURE_THRESHOLD = 3


class RadioClient(Protocol):
    def get_frequency(self) -> int:
        ...

    def set_frequency(self, frequency_hz: int) -> None:
        ...

    def set_mode(self, mode: str, passband_hz: int = 0) -> None:
        ...

    def select_vfo(self, vfo: str) -> None:
        ...


@dataclass(frozen=True)
class RadioDeviceSnapshot:
    enabled: bool
    connected: bool
    write_enabled: bool
    frequency_hz: int | None
    last_read_at_utc: str | None
    last_write_at_utc: str | None
    error: str | None

    def to_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


class RadioManager:
    def __init__(
        self,
        client: RadioClient,
        enabled: bool,
        write_enabled: bool,
        target_vfo: str | None = None,
        failure_threshold: int = DEFAULT_FAILURE_THRESHOLD,
        read_poll_enabled: bool = True,
        restore_vfo_after_write: str | None = None,
    ) -> None:
        self.client = client
        self.enabled = enabled
        self.write_enabled = write_enabled
        self.target_vfo = target_vfo
        self.read_poll_enabled = read_poll_enabled
        self.restore_vfo_after_write = restore_vfo_after_write
        self._lock = RLock()
        self._connected = False
        self._frequency_hz: int | None = None
        self._mode: str | None = None
        self._vfo: str | None = None
        self._last_read_at_utc: str | None = None
        self._last_write_at_utc: str | None = None
        self._error: str | None = None
        self._consecutive_failures = 0
        self._failure_threshold = max(1, int(failure_threshold))

    def _select_vfo_locked(self, vfo: str | None, source: str = "") -> None:
        normalized_vfo = normalize_hamlib_vfo(vfo)
        if normalized_vfo is None:
            return
        LOGGER.info(
            "cat_command source=%s op=set_vfo vfo=%s",
            source or "unknown",
            normalized_vfo,
        )
        self.client.select_vfo(normalized_vfo)
        self._vfo = normalized_vfo

    def snapshot(self) -> RadioDeviceSnapshot:
        with self._lock:
            return RadioDeviceSnapshot(
                enabled=self.enabled,
                connected=self._connected,
                write_enabled=self.write_enabled,
                frequency_hz=self._frequency_hz,
                last_read_at_utc=self._last_read_at_utc,
                last_write_at_utc=self._last_write_at_utc,
                error=self._error,
            )

    def get_frequency(self) -> int:
        with self._lock:
            try:
                if hasattr(self.client, "get_frequency_on_vfo"):
                    frequency_hz = self.client.get_frequency_on_vfo(
                        normalize_hamlib_vfo(self.target_vfo)
                    )
                    self._vfo = normalize_hamlib_vfo(self.target_vfo)
                else:
                    self._select_vfo_locked(self.target_vfo, source="radio_manager.get_frequency")
                    frequency_hz = self.client.get_frequency()
            except Exception as exc:
                self._record_error(exc)
                raise

            was_connected = self._connected
            self._connected = True
            self._frequency_hz = frequency_hz
            self._last_read_at_utc = _utc_now()
            self._error = None
            self._consecutive_failures = 0
        if not was_connected:
            LOGGER.info("Radio connection restored")
        return frequency_hz

    def set_frequency(
        self,
        frequency_hz: int,
        source: str = "",
    ) -> RadioDeviceSnapshot:
        if not self.write_enabled:
            return self.snapshot()
        if frequency_hz <= 0:
            raise ValueError("frequency_hz must be a positive integer")

        with self._lock:
            try:
                LOGGER.info(
                    "cat_command source=%s op=set_frequency target_hz=%s",
                    source or "unknown",
                    frequency_hz,
                )
                normalized_vfo = normalize_hamlib_vfo(self.target_vfo)
                restore_vfo = normalize_hamlib_vfo(self.restore_vfo_after_write)
                if restore_vfo and hasattr(self.client, "set_frequency_on_vfo_and_restore"):
                    self.client.set_frequency_on_vfo_and_restore(
                        normalized_vfo,
                        frequency_hz,
                        restore_vfo,
                    )
                    self._vfo = restore_vfo
                elif hasattr(self.client, "set_frequency_on_vfo"):
                    self.client.set_frequency_on_vfo(normalized_vfo, frequency_hz)
                    self._vfo = normalized_vfo
                else:
                    self._select_vfo_locked(
                        self.target_vfo,
                        source=source or "radio_manager.set_frequency",
                    )
                    self.client.set_frequency(frequency_hz)
            except Exception as exc:
                self._record_error(exc)
                raise

            was_connected = self._connected
            self._connected = True
            self._frequency_hz = frequency_hz
            self._last_write_at_utc = _utc_now()
            self._error = None
            self._consecutive_failures = 0
        if not was_connected:
            LOGGER.info("Radio write connection restored")
        return self.snapshot()

    def try_set_frequency(
        self,
        frequency_hz: int,
        source: str = "",
    ) -> RadioDeviceSnapshot:
        try:
            return self.set_frequency(frequency_hz, source=source)
        except ValueError:
            raise
        except Exception:
            return self.snapshot()

    def set_mode(
        self,
        mode: str,
        passband_hz: int = 0,
        source: str = "",
    ) -> RadioDeviceSnapshot:
        normalized_mode = normalize_hamlib_mode(mode)
        if not self.write_enabled or normalized_mode is None:
            return self.snapshot()
        with self._lock:
            if self._mode == normalized_mode:
                return self.snapshot()

            try:
                LOGGER.info(
                    "cat_command source=%s op=set_mode mode=%s passband_hz=%s",
                    source or "unknown",
                    normalized_mode,
                    passband_hz,
                )
                normalized_vfo = normalize_hamlib_vfo(self.target_vfo)
                restore_vfo = normalize_hamlib_vfo(self.restore_vfo_after_write)
                if restore_vfo and hasattr(self.client, "set_mode_on_vfo_and_restore"):
                    self.client.set_mode_on_vfo_and_restore(
                        normalized_vfo,
                        normalized_mode,
                        passband_hz,
                        restore_vfo,
                    )
                    self._vfo = restore_vfo
                elif hasattr(self.client, "set_mode_on_vfo"):
                    self.client.set_mode_on_vfo(
                        normalized_vfo,
                        normalized_mode,
                        passband_hz,
                    )
                    self._vfo = normalized_vfo
                else:
                    self._select_vfo_locked(
                        self.target_vfo,
                        source=source or "radio_manager.set_mode",
                    )
                    self.client.set_mode(normalized_mode, passband_hz)
            except Exception as exc:
                self._record_error(exc)
                raise

            was_connected = self._connected
            self._connected = True
            self._mode = normalized_mode
            self._last_write_at_utc = _utc_now()
            self._error = None
            self._consecutive_failures = 0
        if not was_connected:
            LOGGER.info("Radio mode connection restored")
        return self.snapshot()

    def try_set_mode(
        self,
        mode: str,
        passband_hz: int = 0,
        source: str = "",
    ) -> RadioDeviceSnapshot:
        try:
            return self.set_mode(mode, passband_hz=passband_hz, source=source)
        except Exception:
            return self.snapshot()

    def set_vfo(self, vfo: str | None, source: str = "") -> RadioDeviceSnapshot:
        normalized_vfo = normalize_hamlib_vfo(vfo)
        if not self.write_enabled or normalized_vfo is None:
            return self.snapshot()
        with self._lock:
            if self._vfo == normalized_vfo:
                return self.snapshot()

            try:
                self._select_vfo_locked(normalized_vfo, source=source or "radio_manager.set_vfo")
            except Exception as exc:
                self._record_error(exc)
                raise

            was_connected = self._connected
            self._connected = True
            self._vfo = normalized_vfo
            self._last_write_at_utc = _utc_now()
            self._error = None
            self._consecutive_failures = 0
        if not was_connected:
            LOGGER.info("Radio VFO connection restored")
        return self.snapshot()

    def try_set_vfo(self, vfo: str | None, source: str = "") -> RadioDeviceSnapshot:
        try:
            return self.set_vfo(vfo, source=source)
        except Exception:
            return self.snapshot()

    def poll_once(self) -> RadioDeviceSnapshot:
        if not self.read_poll_enabled:
            return self.snapshot()
        try:
            self.get_frequency()
        except Exception:
            pass
        return self.snapshot()

    def _record_error(self, exc: Exception) -> None:
        with self._lock:
            self._consecutive_failures += 1
            if self._consecutive_failures < self._failure_threshold:
                return
            previous_error = self._error
            self._connected = False
            self._error = str(exc)
        if previous_error != str(exc):
            LOGGER.warning("Radio operation failed: %s", exc)


def disabled_radio_snapshot() -> RadioDeviceSnapshot:
    return RadioDeviceSnapshot(
        enabled=False,
        connected=False,
        write_enabled=False,
        frequency_hz=None,
        last_read_at_utc=None,
        last_write_at_utc=None,
        error="TX radio is disabled in pi-sat-controller.conf",
    )


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def normalize_hamlib_mode(mode: str | None) -> str | None:
    value = (mode or "").strip().upper()
    if not value:
        return None
    first = value.split()[0]
    mapping = {
        "FMN": "FM",
        "NFM": "FM",
        "FM-N": "FM",
        "FM-W": "WFM",
        "WIDEFM": "WFM",
        "PKT": "PKTFM",
        "PACKET": "PKTFM",
    }
    return mapping.get(first, first)


def normalize_hamlib_vfo(vfo: str | None) -> str | None:
    value = (vfo or "").strip().upper()
    if not value or value == "CURRENT":
        return None
    mapping = {
        "A": "VFOA",
        "B": "VFOB",
        "VFOA": "VFOA",
        "VFOB": "VFOB",
    }
    return mapping.get(value, value)
