from __future__ import annotations

from dataclasses import replace

from pi_sat_controller.backend.radio.radio_manager import disabled_radio_snapshot
from pi_sat_controller.backend.rotator.rotator_manager import (
    disabled_rotator_snapshot,
)
from pi_sat_controller.backend.sdr.polling_sdr import disabled_sdr_snapshot


class DisabledTrackingSdrManager:
    """Fallback object used when SDR polling is disabled in config."""

    def snapshot(self):
        return disabled_sdr_snapshot()

    def set_frequency(self, frequency_hz: int):
        return disabled_sdr_snapshot()

    def try_set_frequency(self, frequency_hz: int):
        return disabled_sdr_snapshot()


class FailedTrackingSdrManager:
    """Fallback RX manager used when startup cannot build the real device path."""

    def __init__(self, error: str) -> None:
        self.error = error

    def start(self) -> None:
        return

    def stop(self) -> None:
        return

    def snapshot(self):
        snapshot = disabled_sdr_snapshot()
        return replace(
            snapshot,
            enabled=True,
            error=self.error,
        )

    def set_frequency(self, frequency_hz: int):
        raise RuntimeError(self.error)

    def try_set_frequency(self, frequency_hz: int):
        return self.snapshot()


class FailedRadioManager:
    """Fallback TX manager used when startup cannot build the real device path."""

    def __init__(self, error: str) -> None:
        self.error = error
        self.client = None

    def snapshot(self):
        snapshot = disabled_radio_snapshot()
        return replace(
            snapshot,
            enabled=True,
            error=self.error,
        )

    def poll_once(self):
        return self.snapshot()

    def set_frequency(self, frequency_hz: int, source: str = ""):
        raise RuntimeError(self.error)

    def try_set_frequency(self, frequency_hz: int, source: str = ""):
        return self.snapshot()

    def set_mode(self, mode: str, passband_hz: int = 0, source: str = ""):
        raise RuntimeError(self.error)

    def try_set_mode(self, mode: str, passband_hz: int = 0, source: str = ""):
        return self.snapshot()

    def set_vfo(self, vfo: str | None, source: str = ""):
        raise RuntimeError(self.error)

    def try_set_vfo(self, vfo: str | None, source: str = ""):
        return self.snapshot()


class FailedRotatorManager:
    """Fallback rotator manager used when startup cannot build the real device path."""

    def __init__(self, error: str, enabled: bool, write_enabled: bool) -> None:
        self.error = error
        self.enabled = enabled
        self.write_enabled = write_enabled
        self.client = None
        self._pass_active = False

    def _snapshot(self):
        return {
            **disabled_rotator_snapshot().to_dict(),
            "enabled": self.enabled,
            "write_enabled": self.write_enabled,
            "pass_active": self._pass_active,
            "manual_controls_enabled": False,
            "state_label": "Rotator unavailable",
            "error": self.error,
        }

    def snapshot(self):
        return disabled_rotator_snapshot().__class__(**self._snapshot())

    def poll_once(self):
        return self.snapshot()

    def set_pass_active(
        self,
        active: bool,
        target_azimuth_deg: float | None = None,
        target_elevation_deg: float | None = None,
    ):
        self._pass_active = active
        return self.snapshot()

    def track_position(self, azimuth_deg: float, elevation_deg: float):
        return self.snapshot()

    def manual_position(self, azimuth_deg: float, elevation_deg: float):
        return self.snapshot()

    def send_home(self):
        return self.snapshot()

    def stop(self):
        return self.snapshot()
