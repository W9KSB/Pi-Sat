from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import logging
from threading import RLock
from typing import Any, Protocol

LOGGER = logging.getLogger(__name__)


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
    ) -> None:
        self.client = client
        self.enabled = enabled
        self.write_enabled = write_enabled
        self.target_vfo = target_vfo
        self._lock = RLock()
        self._connected = False
        self._frequency_hz: int | None = None
        self._mode: str | None = None
        self._vfo: str | None = None
        self._last_read_at_utc: str | None = None
        self._last_write_at_utc: str | None = None
        self._error: str | None = None

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
                frequency_hz = self.client.get_frequency()
            except Exception as exc:
                self._record_error(exc)
                raise

            was_connected = self._connected
            self._connected = True
            self._frequency_hz = frequency_hz
            self._last_read_at_utc = _utc_now()
            self._error = None
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
                self.client.set_frequency(frequency_hz)
            except Exception as exc:
                self._record_error(exc)
                raise

            was_connected = self._connected
            self._connected = True
            self._frequency_hz = frequency_hz
            self._last_write_at_utc = _utc_now()
            self._error = None
        if not was_connected:
            LOGGER.info("Radio write connection restored")
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
                self.client.set_mode(normalized_mode, passband_hz)
            except Exception as exc:
                self._record_error(exc)
                raise

            was_connected = self._connected
            self._connected = True
            self._mode = normalized_mode
            self._last_write_at_utc = _utc_now()
            self._error = None
        if not was_connected:
            LOGGER.info("Radio mode connection restored")
        return self.snapshot()

    def set_vfo(self, vfo: str | None, source: str = "") -> RadioDeviceSnapshot:
        normalized_vfo = normalize_hamlib_vfo(vfo)
        if not self.write_enabled or normalized_vfo is None:
            return self.snapshot()
        with self._lock:
            if self._vfo == normalized_vfo:
                return self.snapshot()

            try:
                LOGGER.info(
                    "cat_command source=%s op=set_vfo vfo=%s",
                    source or "unknown",
                    normalized_vfo,
                )
                self.client.select_vfo(normalized_vfo)
            except Exception as exc:
                self._record_error(exc)
                raise

            was_connected = self._connected
            self._connected = True
            self._vfo = normalized_vfo
            self._last_write_at_utc = _utc_now()
            self._error = None
        if not was_connected:
            LOGGER.info("Radio VFO connection restored")
        return self.snapshot()

    def poll_once(self) -> RadioDeviceSnapshot:
        try:
            self.get_frequency()
        except Exception:
            pass
        return self.snapshot()

    def _record_error(self, exc: Exception) -> None:
        with self._lock:
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
