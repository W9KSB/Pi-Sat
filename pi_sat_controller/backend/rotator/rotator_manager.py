from __future__ import annotations

"""Rotator control policy for pass tracking and manual moves.

This manager keeps the rotator state shown in the UI, decides when manual
control is allowed, suppresses tracking commands below the configured minimum
elevation, and can optionally send the rotator home when a tracked pass ends.
"""

from dataclasses import dataclass
from datetime import datetime, timezone
from threading import Lock
from typing import Any

from pi_sat_controller.backend.rotator.rotctld_client import RotctldClient


@dataclass(frozen=True)
class RotatorSnapshot:
    enabled: bool
    write_enabled: bool
    connected: bool
    pass_active: bool
    manual_controls_enabled: bool
    state_label: str
    current_azimuth_deg: float | None
    current_elevation_deg: float | None
    target_azimuth_deg: float | None
    target_elevation_deg: float | None
    last_read_at_utc: str | None
    last_write_at_utc: str | None
    error: str | None

    def to_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


class RotatorManager:
    """Coordinates pass-driven and manual rotator control through rotctld."""

    def __init__(
        self,
        client: RotctldClient,
        enabled: bool,
        write_enabled: bool,
        min_elevation_deg: float = 0.0,
        home_azimuth_deg: float = 0.0,
        home_elevation_deg: float = 0.0,
        return_home_after_pass: bool = False,
    ) -> None:
        self.client = client
        self.enabled = enabled
        self.write_enabled = write_enabled
        self.min_elevation_deg = min_elevation_deg
        self.home_azimuth_deg = home_azimuth_deg
        self.home_elevation_deg = home_elevation_deg
        self.return_home_after_pass = return_home_after_pass
        self._lock = Lock()
        self._snapshot = RotatorSnapshot(
            enabled=enabled,
            write_enabled=write_enabled,
            connected=False,
            pass_active=False,
            manual_controls_enabled=enabled,
            state_label="NO ACTIVE PASS, manual mode enabled",
            current_azimuth_deg=None,
            current_elevation_deg=None,
            target_azimuth_deg=None,
            target_elevation_deg=None,
            last_read_at_utc=None,
            last_write_at_utc=None,
            error=None,
        )

    def snapshot(self) -> RotatorSnapshot:
        with self._lock:
            return self._snapshot

    def poll_once(self) -> RotatorSnapshot:
        if not self.enabled:
            return self.snapshot()
        try:
            position = self.client.get_position()
        except Exception as exc:
            self._set_error(str(exc))
            return self.snapshot()

        with self._lock:
            self._snapshot = RotatorSnapshot(
                enabled=self.enabled,
                write_enabled=self.write_enabled,
                connected=True,
                pass_active=self._snapshot.pass_active,
                manual_controls_enabled=self._snapshot.manual_controls_enabled,
                state_label=self._snapshot.state_label,
                current_azimuth_deg=round(position.azimuth_deg, 2),
                current_elevation_deg=round(position.elevation_deg, 2),
                target_azimuth_deg=self._snapshot.target_azimuth_deg,
                target_elevation_deg=self._snapshot.target_elevation_deg,
                last_read_at_utc=_utc_now(),
                last_write_at_utc=self._snapshot.last_write_at_utc,
                error=None,
            )
        return self.snapshot()

    def set_pass_active(
        self,
        active: bool,
        target_azimuth_deg: float | None = None,
        target_elevation_deg: float | None = None,
    ) -> RotatorSnapshot:
        """Updates pass state and optionally returns the rotator home on pass end."""

        should_send_home = False
        with self._lock:
            was_active = self._snapshot.pass_active
            self._snapshot = _replace_snapshot(
                self._snapshot,
                pass_active=active,
                manual_controls_enabled=self.enabled and not active,
                state_label=(
                    "PASS ACTIVE, manual mode disabled"
                    if active
                    else "NO ACTIVE PASS, manual mode enabled"
                ),
                target_azimuth_deg=(
                    round(target_azimuth_deg, 2)
                    if target_azimuth_deg is not None
                    else self._snapshot.target_azimuth_deg
                ),
                target_elevation_deg=(
                    round(target_elevation_deg, 2)
                    if target_elevation_deg is not None
                    else self._snapshot.target_elevation_deg
                ),
                error=None if not active and self._snapshot.error == "Target below rotator minimum elevation" else self._snapshot.error,
            )
            should_send_home = (
                was_active
                and not active
                and self.return_home_after_pass
            )
        if should_send_home:
            return self.send_home()
        return self.snapshot()

    def track_position(self, azimuth_deg: float, elevation_deg: float) -> RotatorSnapshot:
        """Sends a pass-tracking target when the rotator is enabled and writable."""

        if not self.enabled:
            return self.snapshot()
        self.set_pass_active(True, azimuth_deg, elevation_deg)
        if elevation_deg < self.min_elevation_deg:
            with self._lock:
                self._snapshot = _replace_snapshot(
                    self._snapshot,
                    target_azimuth_deg=round(azimuth_deg, 2),
                    target_elevation_deg=round(elevation_deg, 2),
                    error="Target below rotator minimum elevation",
                )
            return self.snapshot()
        if not self.write_enabled:
            with self._lock:
                self._snapshot = _replace_snapshot(
                    self._snapshot,
                    target_azimuth_deg=round(azimuth_deg, 2),
                    target_elevation_deg=round(elevation_deg, 2),
                    error="Rotator writes are disabled",
                )
            return self.snapshot()

        try:
            self.client.set_position(azimuth_deg, elevation_deg)
        except Exception as exc:
            self._set_error(str(exc))
            return self.snapshot()

        with self._lock:
            self._snapshot = _replace_snapshot(
                self._snapshot,
                connected=True,
                target_azimuth_deg=round(azimuth_deg, 2),
                target_elevation_deg=round(elevation_deg, 2),
                last_write_at_utc=_utc_now(),
                error=None,
            )
        return self.snapshot()

    def manual_position(self, azimuth_deg: float, elevation_deg: float) -> RotatorSnapshot:
        """Sends a manual rotator move when no active pass currently owns control."""

        if not self.enabled:
            return self.snapshot()
        if not self.write_enabled:
            with self._lock:
                self._snapshot = _replace_snapshot(
                    self._snapshot,
                    target_azimuth_deg=round(azimuth_deg, 2),
                    target_elevation_deg=round(elevation_deg, 2),
                    error="Rotator writes are disabled",
                )
            return self.snapshot()
        with self._lock:
            if self._snapshot.pass_active:
                self._snapshot = _replace_snapshot(
                    self._snapshot,
                    manual_controls_enabled=False,
                    state_label="PASS ACTIVE, manual mode disabled",
                    error="PASS ACTIVE, manual mode disabled",
                )
                return self._snapshot

        try:
            self.client.set_position(azimuth_deg, elevation_deg)
        except Exception as exc:
            self._set_error(str(exc))
            return self.snapshot()

        with self._lock:
            self._snapshot = _replace_snapshot(
                self._snapshot,
                connected=True,
                pass_active=False,
                manual_controls_enabled=True,
                state_label="NO ACTIVE PASS, manual mode enabled",
                target_azimuth_deg=round(azimuth_deg, 2),
                target_elevation_deg=round(elevation_deg, 2),
                last_write_at_utc=_utc_now(),
                error=None,
            )
        return self.snapshot()

    def send_home(self) -> RotatorSnapshot:
        return self.manual_position(self.home_azimuth_deg, self.home_elevation_deg)

    def stop(self) -> RotatorSnapshot:
        if not self.enabled:
            return self.snapshot()
        try:
            self.client.stop()
        except Exception as exc:
            self._set_error(str(exc))
            return self.snapshot()
        with self._lock:
            self._snapshot = _replace_snapshot(
                self._snapshot,
                connected=True,
                error=None,
            )
        return self.snapshot()

    def _set_error(self, error: str) -> None:
        with self._lock:
            self._snapshot = _replace_snapshot(
                self._snapshot,
                connected=False,
                error=error,
            )


def disabled_rotator_snapshot() -> RotatorSnapshot:
    return RotatorSnapshot(
        enabled=False,
        write_enabled=False,
        connected=False,
        pass_active=False,
        manual_controls_enabled=False,
        state_label="Rotator control is off",
        current_azimuth_deg=None,
        current_elevation_deg=None,
        target_azimuth_deg=None,
        target_elevation_deg=None,
        last_read_at_utc=None,
        last_write_at_utc=None,
        error="Rotator control is off or not configured",
    )


def _replace_snapshot(snapshot: RotatorSnapshot, **updates: Any) -> RotatorSnapshot:
    values = snapshot.__dict__.copy()
    values.update(updates)
    return RotatorSnapshot(**values)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()
