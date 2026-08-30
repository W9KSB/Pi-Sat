from __future__ import annotations

import logging
from contextlib import contextmanager
from threading import RLock
from typing import Iterator

from pi_sat_controller.backend.radio.local_hamlib_client import LocalHamlibClient
from pi_sat_controller.backend.radio.radio_manager import (
    RadioOperationDeferred,
    normalize_hamlib_vfo,
)

LOGGER = logging.getLogger(__name__)
class SharedLocalRadioController:
    """Serializes all CAT operations for one local radio serving RX and TX."""

    def __init__(
        self,
        client: LocalHamlibClient,
        rx_vfo: str | None,
        tx_vfo: str | None,
        split_enabled: bool,
    ) -> None:
        self.client = client
        self.rx_vfo = normalize_hamlib_vfo(rx_vfo)
        self.tx_vfo = normalize_hamlib_vfo(tx_vfo)
        self.split_enabled = split_enabled
        self._lock = RLock()
        self._configured_generation = -1
        self._ptt_warning_logged = False

        if self.rx_vfo is None or self.tx_vfo is None:
            raise ValueError(
                "Shared local RX/TX control requires explicit RX and TX VFO selections."
            )
        if self.rx_vfo == self.tx_vfo:
            raise ValueError("Shared local RX and TX must use different VFOs.")
        self.client.register_role_vfo("rx", self.rx_vfo)
        self.client.register_role_vfo("tx", self.tx_vfo)

    def initialize(self) -> None:
        with self._lock:
            self._ensure_configured_locked()

    def close(self) -> None:
        self.client.close()

    def get_frequency(self, role: str) -> int:
        normalized_role = self._normalize_role(role)
        with self._lock:
            self._ensure_configured_locked()
            target_vfo = self.rx_vfo if normalized_role == "rx" else self.tx_vfo
            return self.client.get_frequency_on_vfo(target_vfo)

    def set_frequency(self, role: str, frequency_hz: int) -> None:
        normalized_role = self._normalize_role(role)
        with self._lock:
            configured_now = self._ensure_configured_locked()
            # RX frequency remains adjustable during full-duplex transmit so
            # Doppler tracking and Virtual RIT can keep the receive VFO useful.
            if normalized_role == "tx" and not configured_now:
                self._defer_if_transmitting_locked(
                    f"{normalized_role.upper()} frequency write"
                )
            target_vfo = self.rx_vfo if normalized_role == "rx" else self.tx_vfo
            self.client.set_frequency_on_vfo(target_vfo, frequency_hz)

    def set_mode(self, role: str, mode: str, passband_hz: int = 0) -> None:
        normalized_role = self._normalize_role(role)
        with self._lock:
            configured_now = self._ensure_configured_locked()
            if not configured_now:
                self._defer_if_transmitting_locked(f"{normalized_role.upper()} mode write")
            target_vfo = self.rx_vfo if normalized_role == "rx" else self.tx_vfo
            self.client.set_mode_on_vfo(target_vfo, mode, passband_hz)

    def set_ctcss_tone(self, role: str, tone_tenths_hz: int) -> None:
        normalized_role = self._normalize_role(role)
        with self._lock:
            configured_now = self._ensure_configured_locked()
            if not configured_now:
                self._defer_if_transmitting_locked(
                    f"{normalized_role.upper()} CTCSS tone write"
                )
            target_vfo = self.rx_vfo if normalized_role == "rx" else self.tx_vfo
            self.client.set_ctcss_tone_on_vfo(target_vfo, tone_tenths_hz)

    def set_tone_enabled(self, role: str, enabled: bool) -> None:
        normalized_role = self._normalize_role(role)
        with self._lock:
            configured_now = self._ensure_configured_locked()
            if not configured_now:
                self._defer_if_transmitting_locked(
                    f"{normalized_role.upper()} CTCSS encoder write"
                )
            target_vfo = self.rx_vfo if normalized_role == "rx" else self.tx_vfo
            self.client.set_tone_enabled_on_vfo(target_vfo, enabled)

    @contextmanager
    def operation_batch(self) -> Iterator[None]:
        """Ensures shared-radio setup before a serialized tracking cycle."""

        with self._lock:
            self._ensure_configured_locked()
        yield

    def select_role_vfo(self, role: str) -> None:
        normalized_role = self._normalize_role(role)
        LOGGER.info(
            "shared_radio op=set_vfo role=%s skipped=vfo_addressed_mode",
            normalized_role,
        )

    def enable_split(self) -> None:
        with self._lock:
            self._ensure_configured_locked(force_split=True)

    def disable_split(self) -> None:
        with self._lock:
            self._defer_if_transmitting_locked("shared radio split disable")
            self.client.set_split_on_vfo(self.rx_vfo, False, None)

    def _ensure_configured_locked(self, force_split: bool = False) -> bool:
        generation = self.client.ensure_connected()
        if generation == self._configured_generation and not force_split:
            return False
        self._defer_if_transmitting_locked("shared radio initialization")
        if self.split_enabled:
            try:
                self.client.set_split_on_vfo(self.rx_vfo, True, self.tx_vfo)
            except RuntimeError as exc:
                if "RPRT -9" not in str(exc):
                    raise
                self.split_enabled = False
                LOGGER.warning(
                    "Shared local radio rejected split mode; continuing with "
                    "VFO-addressed RX/TX control."
                )
        self._configured_generation = generation
        LOGGER.info(
            "Shared local radio configured rx_vfo=%s tx_vfo=%s split=%s",
            self.rx_vfo,
            self.tx_vfo,
            self.split_enabled,
        )
        return True

    def _defer_if_transmitting_locked(self, operation: str) -> None:
        try:
            transmitting = self.client.get_ptt_on_vfo(self.tx_vfo)
        except RuntimeError as exc:
            if "RPRT -" not in str(exc):
                raise
            if not self._ptt_warning_logged:
                LOGGER.warning(
                    "Shared local radio PTT read is currently unavailable; "
                    "CAT operation deferred."
                )
                self._ptt_warning_logged = True
            raise RadioOperationDeferred(
                f"{operation} deferred because PTT state is unavailable."
            ) from exc
        self._ptt_warning_logged = False
        if transmitting:
            raise RadioOperationDeferred(f"{operation} deferred while radio is transmitting.")

    @staticmethod
    def _normalize_role(role: str) -> str:
        normalized = role.strip().lower()
        if normalized not in {"rx", "tx"}:
            raise ValueError(f"Unsupported shared radio role: {role}")
        return normalized

class SharedRadioRoleClient:
    """Presents one side of a shared radio through the normal client interface."""

    def __init__(self, controller: SharedLocalRadioController, role: str) -> None:
        self.controller = controller
        self.role = controller._normalize_role(role)

    def close(self) -> None:
        self.controller.close()

    def ensure_connected(self) -> int:
        self.controller.initialize()
        return self.controller.client.connection_generation

    def operation_batch(self):
        return self.controller.operation_batch()

    def drain_radio_state_events(self):
        return self.controller.client.drain_radio_state_events(self.role)

    def is_async_property_verified(self, property) -> bool:
        return self.controller.client.is_async_property_verified(property, self.role)

    def mark_async_property_unverified(self, property, *, reason: str) -> None:
        self.controller.client.mark_async_property_unverified(
            property,
            reason=reason,
            role=self.role,
        )

    def async_status(self) -> dict[str, object]:
        return self.controller.client.async_status(self.role)

    def get_frequency(self) -> int:
        return self.controller.get_frequency(self.role)

    def get_frequency_on_vfo(self, _vfo: str | None) -> int:
        return self.controller.get_frequency(self.role)

    def set_frequency(self, frequency_hz: int) -> None:
        self.controller.set_frequency(self.role, frequency_hz)

    def set_frequency_on_vfo(self, _vfo: str | None, frequency_hz: int) -> None:
        self.controller.set_frequency(self.role, frequency_hz)

    def set_mode(self, mode: str, passband_hz: int = 0) -> None:
        self.controller.set_mode(self.role, mode, passband_hz)

    def set_mode_on_vfo(
        self,
        _vfo: str | None,
        mode: str,
        passband_hz: int = 0,
    ) -> None:
        self.controller.set_mode(self.role, mode, passband_hz)

    def set_ctcss_tone_on_vfo(
        self,
        _vfo: str | None,
        tone_tenths_hz: int,
    ) -> None:
        self.controller.set_ctcss_tone(self.role, tone_tenths_hz)

    def set_tone_enabled_on_vfo(
        self,
        _vfo: str | None,
        enabled: bool,
    ) -> None:
        self.controller.set_tone_enabled(self.role, enabled)

    def select_vfo(self, _vfo: str) -> None:
        self.controller.select_role_vfo(self.role)

    def set_split(self, enabled: bool, _tx_vfo: str | None = None) -> None:
        if enabled:
            self.controller.enable_split()
        else:
            self.controller.disable_split()

    def set_split_frequency(self, frequency_hz: int) -> None:
        self.controller.set_frequency("tx", frequency_hz)

    def set_split_mode(self, mode: str, passband_hz: int = 0) -> None:
        self.controller.set_mode("tx", mode, passband_hz)
