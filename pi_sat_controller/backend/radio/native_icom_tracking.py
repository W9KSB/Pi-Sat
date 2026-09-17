from __future__ import annotations

"""Tracking-role adapters for the single native IC-9700 LAN controller.

These objects deliberately own no sockets, threads, or radio state.  They only
translate the existing tracker interface into explicit MAIN/SUB operations on
the controller already used by the Radio page.
"""

from datetime import datetime, timezone
import logging
from time import monotonic

from pi_sat_controller.backend.radio.icom_radio_controller import IcomRadioController
from pi_sat_controller.backend.radio.radio_manager import (
    RadioDeviceSnapshot,
    normalize_hamlib_mode,
)
from pi_sat_controller.backend.radio.radio_state import (
    RadioFrequencyObservation,
    RadioStateClassification,
)
from pi_sat_controller.backend.sdr.polling_sdr import SdrDeviceSnapshot


LOGGER = logging.getLogger(__name__)


class NativeIcomTrackingRole:
    """Duck-typed RX or TX manager backed by one shared native controller."""

    restore_vfo_after_write = None
    split_mode_vfo = None

    def __init__(
        self,
        controller: IcomRadioController,
        role: str,
        *,
        enabled: bool,
    ) -> None:
        normalized_role = str(role).strip().lower()
        if normalized_role not in {"rx", "tx"}:
            raise ValueError("Native Icom tracking role must be RX or TX")
        self.controller = controller
        self.role = normalized_role
        self.physical_side = "SUB" if normalized_role == "rx" else "MAIN"
        self.enabled = bool(enabled)
        self.write_enabled = bool(enabled)
        # The tracker inspects these compatibility attributes.  They name the
        # physical side, not either side's independently selectable A/B VFO.
        self.target_vfo = self.physical_side
        self.client = controller
        self.radio_manager = self
        self._last_read_at_utc: str | None = None
        self._last_write_at_utc: str | None = None
        self._last_error: str | None = None

    def start(self) -> None:
        """The Radio page's master Connect action owns initial startup."""

    def stop(self) -> None:
        """Runtime dependency swaps must not stop the shared owner."""

    def set_background_polling_enabled(self, enabled: bool) -> None:
        """The shared owner already maintains authoritative telemetry."""

    def connection_generation(self) -> int:
        return self.controller.connection_generation()

    def snapshot(self) -> SdrDeviceSnapshot | RadioDeviceSnapshot:
        payload = self.controller.snapshot()
        side = payload[self.physical_side.lower()]
        error = self._last_error or payload.get("last_error")
        connected = bool(payload.get("connected"))
        if self.enabled and not connected and not error:
            error = "Native IC-9700 is not connected; use Connect on the Radio page."
        values = dict(
            enabled=self.enabled,
            connected=connected,
            frequency_hz=side.get("frequency_hz"),
            last_read_at_utc=self._last_read_at_utc,
            last_write_at_utc=self._last_write_at_utc,
            error=error,
        )
        if self.role == "tx":
            return RadioDeviceSnapshot(write_enabled=self.write_enabled, **values)
        return SdrDeviceSnapshot(**values)

    def get_frequency(self) -> int:
        payload = self.controller.snapshot()
        side = payload[self.physical_side.lower()]
        if not payload.get("connected"):
            raise RuntimeError(
                payload.get("last_error")
                or "Native IC-9700 is not connected; use Connect on the Radio page."
            )
        if side.get("frequency_hz") is None:
            raise RuntimeError(f"Native IC-9700 {self.physical_side} frequency is unavailable")
        self._last_error = None
        self._last_read_at_utc = _utc_now()
        return int(side["frequency_hz"])

    def read_frequency_once(self) -> SdrDeviceSnapshot:
        self.get_frequency()
        snapshot = self.snapshot()
        return SdrDeviceSnapshot(
            enabled=snapshot.enabled,
            connected=snapshot.connected,
            frequency_hz=snapshot.frequency_hz,
            last_read_at_utc=self._last_read_at_utc,
            last_write_at_utc=snapshot.last_write_at_utc,
            error=snapshot.error,
        )

    def poll_once(self) -> SdrDeviceSnapshot | RadioDeviceSnapshot:
        """Refresh the role while retaining its RX- or TX-specific snapshot type."""
        try:
            self.get_frequency()
        except Exception as exc:
            self._last_error = str(exc)
        return self.snapshot()

    def read_frequency_for_reconciliation(self) -> RadioFrequencyObservation:
        return self.get_frequency_for_reconciliation()

    def get_frequency_for_reconciliation(self) -> RadioFrequencyObservation:
        frequency_hz = None
        error = None
        try:
            frequency_hz = self.controller.get_frequency(self.physical_side)
            self._last_error = None
            self._last_read_at_utc = _utc_now()
        except Exception as exc:
            error = self._last_error = str(exc)
        # Fresh readback lets the tracker distinguish manual tuning from its
        # last command using the same offset reconciliation as polled radios.
        return RadioFrequencyObservation(
            frequency_hz=None if frequency_hz is None else int(frequency_hz),
            classification=RadioStateClassification.STATE_REFRESH,
            timestamp=monotonic(),
            from_poll=True,
            error=error,
        )

    def set_frequency(self, frequency_hz: int, source: str = ""):
        if not self.write_enabled:
            return self.snapshot()
        try:
            LOGGER.info(
                "cat_command source=%s owner=native_icom role=%s side=%s op=set_frequency target_hz=%s",
                source or "unknown",
                self.role,
                self.physical_side,
                frequency_hz,
            )
            self.controller.set_frequency(self.physical_side, None, int(frequency_hz))
            self._last_write_at_utc = _utc_now()
            self._last_error = None
        except Exception as exc:
            self._last_error = str(exc)
            raise
        return self.snapshot()

    def try_set_frequency(self, frequency_hz: int, source: str = ""):
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
        force: bool = False,
    ):
        normalized = normalize_hamlib_mode(mode)
        native_mode = {
            "CWR": "CW-R",
            "PKTFM": "FM",
            "WFM": "FM",
        }.get(normalized or "", normalized)
        if native_mode is None:
            return self.snapshot()
        current_mode = self.controller.snapshot()[self.physical_side.lower()].get("mode")
        if current_mode == native_mode:
            return self.snapshot()
        try:
            LOGGER.info(
                "cat_command source=%s owner=native_icom role=%s side=%s op=set_mode mode=%s",
                source or "unknown",
                self.role,
                self.physical_side,
                native_mode,
            )
            self.controller.set_mode(self.physical_side, native_mode, None)
            self._last_write_at_utc = _utc_now()
            self._last_error = None
        except Exception as exc:
            self._last_error = str(exc)
            raise
        return self.snapshot()

    def try_set_mode(self, mode: str, passband_hz: int = 0, source: str = "", force: bool = False):
        try:
            return self.set_mode(mode, passband_hz, source, force)
        except ValueError:
            raise
        except Exception:
            return self.snapshot()

    def set_vfo(self, vfo: str | None, source: str = ""):
        # MAIN/SUB are the routing boundary.  Both physical sides may currently
        # use VFO A, so tracking must never reinterpret this as an A/B selection.
        return self.snapshot()

    def try_set_vfo(self, vfo: str | None, source: str = ""):
        return self.set_vfo(vfo, source)

    def try_set_ctcss_tone(self, tone_hz: float | None, source: str = ""):
        if tone_hz is not None:
            self._last_error = "Native IC-9700 profile CTCSS setup is not supported yet."
        return self.snapshot()


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()
