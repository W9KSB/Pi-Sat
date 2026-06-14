from __future__ import annotations

import logging
from threading import RLock
from time import sleep

from pi_sat_controller.backend.radio.local_hamlib_client import LocalHamlibClient
from pi_sat_controller.backend.radio.radio_manager import (
    RadioOperationDeferred,
    normalize_hamlib_vfo,
)

LOGGER = logging.getLogger(__name__)
WRITE_SETTLE_S = 0.05
VERIFY_TOLERANCE_HZ = 100


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
        self._ptt_supported = True

        if self.rx_vfo is None or self.tx_vfo is None:
            raise ValueError(
                "Shared local RX/TX control requires explicit RX and TX VFO selections."
            )
        if self.rx_vfo == self.tx_vfo:
            raise ValueError("Shared local RX and TX must use different VFOs.")

    def close(self) -> None:
        self.client.close()

    def get_frequency(self, role: str) -> int:
        normalized_role = self._normalize_role(role)
        with self._lock:
            self._ensure_configured_locked()
            if normalized_role == "rx":
                self._defer_if_transmitting_locked("RX read")
            target_vfo = self.rx_vfo if normalized_role == "rx" else self.tx_vfo
            return self.client.get_frequency_on_vfo(target_vfo)

    def set_frequency(self, role: str, frequency_hz: int) -> None:
        normalized_role = self._normalize_role(role)
        with self._lock:
            self._ensure_configured_locked()
            self._defer_if_transmitting_locked(f"{normalized_role.upper()} frequency write")
            target_vfo = self.rx_vfo if normalized_role == "rx" else self.tx_vfo
            self.client.set_frequency_on_vfo(target_vfo, frequency_hz)
            sleep(WRITE_SETTLE_S)
            readback_hz = self.client.get_frequency_on_vfo(target_vfo)
            if abs(readback_hz - frequency_hz) > VERIFY_TOLERANCE_HZ:
                raise RuntimeError(
                    f"{normalized_role.upper()} frequency verification failed: "
                    f"requested {frequency_hz} Hz, read {readback_hz} Hz"
                )

    def set_mode(self, role: str, mode: str, passband_hz: int = 0) -> None:
        normalized_role = self._normalize_role(role)
        with self._lock:
            self._ensure_configured_locked()
            self._defer_if_transmitting_locked(f"{normalized_role.upper()} mode write")
            target_vfo = self.rx_vfo if normalized_role == "rx" else self.tx_vfo
            self.client.set_mode_on_vfo(target_vfo, mode, passband_hz)
            sleep(WRITE_SETTLE_S)

    def select_role_vfo(self, role: str) -> None:
        normalized_role = self._normalize_role(role)
        LOGGER.info(
            "shared_radio op=set_vfo role=%s skipped=vfo_addressed_mode",
            normalized_role,
        )

    def enable_split(self) -> None:
        with self._lock:
            self._ensure_configured_locked(force_split=True)

    def _ensure_configured_locked(self, force_split: bool = False) -> None:
        generation = self.client.ensure_connected()
        if generation == self._configured_generation and not force_split:
            return
        self._defer_if_transmitting_locked("shared radio initialization")
        if self.split_enabled:
            self.client.set_split_on_vfo(self.rx_vfo, True, self.tx_vfo)
        self._configured_generation = generation
        LOGGER.info(
            "Shared local radio configured rx_vfo=%s tx_vfo=%s split=%s",
            self.rx_vfo,
            self.tx_vfo,
            self.split_enabled,
        )

    def _defer_if_transmitting_locked(self, operation: str) -> None:
        if not self._ptt_supported:
            return
        try:
            transmitting = self.client.get_ptt_on_vfo(self.rx_vfo)
        except RuntimeError as exc:
            if "RPRT -" not in str(exc):
                raise
            self._ptt_supported = False
            LOGGER.warning(
                "Shared local radio PTT read is unsupported; CAT operations cannot be PTT-gated."
            )
            return
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

    def select_vfo(self, _vfo: str) -> None:
        self.controller.select_role_vfo(self.role)

    def set_split(self, enabled: bool, _tx_vfo: str | None = None) -> None:
        if enabled:
            self.controller.enable_split()

    def set_split_frequency(self, frequency_hz: int) -> None:
        self.controller.set_frequency("tx", frequency_hz)

    def set_split_mode(self, mode: str, passband_hz: int = 0) -> None:
        self.controller.set_mode("tx", mode, passband_hz)
