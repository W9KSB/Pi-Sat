from __future__ import annotations

"""Rotator control policy for pass tracking and manual moves.

This manager keeps the rotator state shown in the UI, decides when manual
control is allowed, suppresses tracking commands below the configured minimum
elevation, and can optionally send the rotator home when a tracked pass ends.
"""

from dataclasses import dataclass
from datetime import datetime, timezone
import logging
import math
from threading import Lock, RLock
from typing import Any

from pi_sat_controller.backend.config import validate_tracking_threshold_deg
from pi_sat_controller.backend.rotator.rotctld_client import RotctldClient

LOGGER = logging.getLogger(__name__)
DEFAULT_FAILURE_THRESHOLD = 3
# Targets are compared and reported at the same two-decimal precision the
# snapshot already uses, without changing the precision sent to the controller.
TARGET_DECIMALS = 2
# A raw azimuth change larger than this means the target crossed the 0/360
# representation boundary. It is never suppressed as a small move; the command
# keeps the representation the tracker produced and no shortest-path or
# cable-wrap policy is inferred from the change.
AZIMUTH_REPRESENTATION_LIMIT_DEG = 180.0
# Two-decimal degree values are compared with a small tolerance so a threshold
# like 0.1 still sends for 0.20 -> 0.30, whose float difference is slightly low.
THRESHOLD_TOLERANCE_DEG = 1e-9


def format_degrees(value: float) -> str:
    """Formats a degree value for operator-facing status text."""

    return f"{value:g}"


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
    # Backend-authoritative tracking state for the operator display.
    tracking_status: str = "disabled"
    tracking_message: str | None = None

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
        tracking_threshold_deg: float = 0.0,
        home_below_min_elevation: bool = False,
        failure_threshold: int = DEFAULT_FAILURE_THRESHOLD,
    ) -> None:
        self.client = client
        self.enabled = enabled
        self.write_enabled = write_enabled
        self.min_elevation_deg = float(min_elevation_deg)
        self.home_azimuth_deg = home_azimuth_deg
        self.home_elevation_deg = home_elevation_deg
        self.return_home_after_pass = return_home_after_pass
        self.tracking_threshold_deg = validate_tracking_threshold_deg(
            tracking_threshold_deg
        )
        self.home_below_min_elevation = bool(home_below_min_elevation)
        self._lock = Lock()
        self._operation_lock = RLock()
        self._shutting_down = False
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
            tracking_status="idle",
            tracking_message=None,
        )
        self._consecutive_failures = 0
        self._failure_threshold = max(1, int(failure_threshold))
        # Tracking policy state. Every field below is guarded by self._lock and
        # only advanced by writable tracking cycles, so a read-only snapshot
        # refresh can never schedule new motion.
        self._last_commanded_azimuth_deg: float | None = None
        self._last_commanded_elevation_deg: float | None = None
        self._holding_target = False
        self._previous_elevation_deg: float | None = None
        self._previous_eligible: bool | None = None
        self._pass_had_eligible = False
        self._cutoff_home_pending = False
        self._cutoff_home_latched = False
        self._cutoff_home_attempted = False
        self._deferred_pass_end_home = False
        # Live tracker state, reported by the tracking loop each cycle. A
        # satellite above the horizon does not mean tracking is running.
        self._tracking_active = False

    def snapshot(self) -> RotatorSnapshot:
        with self._lock:
            return self._snapshot

    def poll_once(self) -> RotatorSnapshot:
        if not self.enabled:
            return self.snapshot()
        with self._operation_lock:
            if self._shutting_down:
                return self.snapshot()
            try:
                position = self.client.get_position()
            except Exception as exc:
                self._set_error(str(exc))
                return self.snapshot()

        with self._lock:
            was_connected = self._snapshot.connected
            if not was_connected:
                # A reconnected controller may have been moved, so the next
                # eligible target is sent even when it is inside the threshold.
                self._last_commanded_azimuth_deg = None
                self._last_commanded_elevation_deg = None
                self._holding_target = False
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
                tracking_status=self._snapshot.tracking_status,
                tracking_message=self._snapshot.tracking_message,
            )
            self._consecutive_failures = 0
        if not was_connected:
            LOGGER.info("Rotator connection restored")
        return self.snapshot()

    def set_pass_active(
        self,
        active: bool,
        target_azimuth_deg: float | None = None,
        target_elevation_deg: float | None = None,
    ) -> RotatorSnapshot:
        """Updates pass state and optionally returns the rotator home on pass end."""

        with self._lock:
            should_send_home = self._apply_pass_state(
                active, target_azimuth_deg, target_elevation_deg
            )
        if should_send_home:
            return self.send_home()
        return self.snapshot()

    def track_position(self, azimuth_deg: float, elevation_deg: float) -> RotatorSnapshot:
        """Sends a pass-tracking target when the rotator is enabled and writable.

        Tracking cycles use update_tracking so the elevation cutoff and update
        threshold apply; this stays as the explicit single-target entry point.
        """

        return self.update_tracking(azimuth_deg, elevation_deg, write=True)

    def update_tracking(
        self,
        azimuth_deg: float,
        elevation_deg: float,
        *,
        write: bool = True,
        tracking_active: bool = True,
    ) -> RotatorSnapshot:
        """Applies one tracking cycle for the tracked satellite position.

        Writable cycles advance the pass, elevation-cutoff, and update-threshold
        state machine and may command the rotator. Read-only cycles refresh the
        displayed pass state only, so a snapshot refresh never moves hardware.
        ``tracking_active`` reports whether the tracking session is running, so
        a paused preview never claims live tracking.
        """

        if not self.enabled:
            return self.snapshot()

        azimuth_deg = float(azimuth_deg)
        elevation_deg = float(elevation_deg)
        pass_active = bool(elevation_deg >= 0.0)
        eligible = bool(pass_active and elevation_deg >= self.min_elevation_deg)
        motion = bool(write and self.write_enabled)

        if write and not self.write_enabled:
            with self._lock:
                self._snapshot = _replace_snapshot(
                    self._snapshot,
                    target_azimuth_deg=round(azimuth_deg, TARGET_DECIMALS),
                    target_elevation_deg=round(elevation_deg, TARGET_DECIMALS),
                    error="Rotator writes are disabled",
                )

        command: tuple[float, float, bool] | None = None
        with self._operation_lock:
            if self._shutting_down:
                return self.snapshot()
            with self._lock:
                if tracking_active and not self._tracking_active:
                    # Resuming tracking forces the first eligible target out.
                    self._last_commanded_azimuth_deg = None
                    self._last_commanded_elevation_deg = None
                    self._holding_target = False
                self._tracking_active = bool(tracking_active)
                pass_end_home = self._apply_pass_state(
                    pass_active, azimuth_deg, elevation_deg
                )
                if motion:
                    self._advance_cutoff_home_state(elevation_deg, eligible)
                    if self._cutoff_home_pending:
                        # A cutoff home outranks tracking, and the same pending
                        # state retries on later writable cycles until it lands.
                        command = (
                            self.home_azimuth_deg,
                            self.home_elevation_deg,
                            True,
                        )
                    elif pass_active and eligible:
                        if self._should_send_target(azimuth_deg, elevation_deg):
                            command = (azimuth_deg, elevation_deg, False)
                        else:
                            self._holding_target = True
                    elif pass_end_home or self._deferred_pass_end_home:
                        command = (
                            self.home_azimuth_deg,
                            self.home_elevation_deg,
                            True,
                        )
                        self._deferred_pass_end_home = False
                    else:
                        self._holding_target = False
                    if pass_active and not eligible:
                        self._snapshot = _replace_snapshot(
                            self._snapshot,
                            error="Target below rotator minimum elevation",
                        )
                    elif pass_active:
                        self._snapshot = _replace_snapshot(
                            self._snapshot,
                            error=(
                                None
                                if self._snapshot.error
                                == "Target below rotator minimum elevation"
                                else self._snapshot.error
                            ),
                        )
                elif pass_end_home:
                    # The horizon crossing was observed on a read-only cycle;
                    # the home command waits for the next writable cycle.
                    self._deferred_pass_end_home = True
                self._refresh_tracking_status(eligible)

            if command is None:
                return self.snapshot()

            command_azimuth, command_elevation, is_home = command
            try:
                self.client.set_position(command_azimuth, command_elevation)
            except Exception as exc:
                with self._lock:
                    if is_home:
                        # Never latch a home that did not reach the controller.
                        self._cutoff_home_pending = True
                        self._cutoff_home_latched = False
                    self._refresh_tracking_status(eligible)
                self._set_error(str(exc))
                return self.snapshot()

            with self._lock:
                was_connected = self._snapshot.connected
                commanded_azimuth = round(command_azimuth, TARGET_DECIMALS)
                commanded_elevation = round(command_elevation, TARGET_DECIMALS)
                self._holding_target = False
                if is_home:
                    # The home command is not a tracking baseline: the next
                    # eligible target is forced out even when it sits inside
                    # the update threshold.
                    self._last_commanded_azimuth_deg = None
                    self._last_commanded_elevation_deg = None
                    self._cutoff_home_pending = False
                    self._cutoff_home_latched = True
                    self._cutoff_home_attempted = True
                else:
                    self._last_commanded_azimuth_deg = commanded_azimuth
                    self._last_commanded_elevation_deg = commanded_elevation
                self._snapshot = _replace_snapshot(
                    self._snapshot,
                    connected=True,
                    target_azimuth_deg=commanded_azimuth,
                    target_elevation_deg=commanded_elevation,
                    last_write_at_utc=_utc_now(),
                    error=None,
                )
                self._consecutive_failures = 0
                self._refresh_tracking_status(eligible)
            if not was_connected:
                LOGGER.info("Rotator tracking connection restored")
        return self.snapshot()

    def _apply_pass_state(
        self,
        active: bool,
        target_azimuth_deg: float | None,
        target_elevation_deg: float | None,
    ) -> bool:
        """Applies pass state to the snapshot and tracking state.

        Returns True when a pass-end home should be sent. The caller holds the
        lock.
        """

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
                round(target_azimuth_deg, TARGET_DECIMALS)
                if target_azimuth_deg is not None
                else self._snapshot.target_azimuth_deg
            ),
            target_elevation_deg=(
                round(target_elevation_deg, TARGET_DECIMALS)
                if target_elevation_deg is not None
                else self._snapshot.target_elevation_deg
            ),
            error=(
                None
                if not active
                and self._snapshot.error == "Target below rotator minimum elevation"
                else self._snapshot.error
            ),
        )
        if was_active != active:
            # A new pass starts from an unknown commanded position, so the next
            # eligible target is sent even when it sits inside the threshold.
            self._last_commanded_azimuth_deg = None
            self._last_commanded_elevation_deg = None
            self._holding_target = False
        if active and not was_active:
            self._pass_had_eligible = False
            self._cutoff_home_pending = False
            self._cutoff_home_latched = False
            self._cutoff_home_attempted = False
            self._previous_elevation_deg = None
            self._previous_eligible = None
            self._deferred_pass_end_home = False
        should_send_home = (
            was_active
            and not active
            and not self._cutoff_home_attempted
            and (self.return_home_after_pass or self._home_at_horizon_enabled)
        )
        if was_active and not active:
            # The below-cutoff episode is over, but a failed cutoff home keeps
            # retrying through the pending flag until it succeeds.
            self._pass_had_eligible = False
            self._cutoff_home_latched = False
            self._cutoff_home_attempted = False
            self._holding_target = False
        return should_send_home

    @property
    def _home_at_horizon_enabled(self) -> bool:
        """True when the cutoff home coincides with the horizon crossing."""

        return bool(self.home_below_min_elevation and self.min_elevation_deg <= 0.0)

    def _advance_cutoff_home_state(
        self, elevation_deg: float, eligible: bool
    ) -> None:
        """Advances cutoff-homing state from one writable tracking sample.

        Only writable cycles call this, so a read-only refresh cannot schedule a
        new home. The caller holds the lock.
        """

        if eligible:
            if self._previous_eligible is not True:
                # Entering or re-entering the tracking window forces the next
                # target out even when it sits inside the update threshold.
                self._last_commanded_azimuth_deg = None
                self._last_commanded_elevation_deg = None
            self._pass_had_eligible = True
            self._cutoff_home_pending = False
            self._cutoff_home_latched = False
        elif self.home_below_min_elevation and self.min_elevation_deg > 0.0:
            previous = self._previous_elevation_deg
            if previous is None:
                # Started below the cutoff mid-pass; wait until the target has
                # been inside the tracking window before homing.
                self._cutoff_home_latched = True
                self._cutoff_home_pending = False
            elif (
                previous >= self.min_elevation_deg
                and not self._cutoff_home_latched
            ):
                self._cutoff_home_pending = True
        self._previous_elevation_deg = elevation_deg
        self._previous_eligible = eligible

    def _should_send_target(self, azimuth_deg: float, elevation_deg: float) -> bool:
        """Returns True when a tracking target should be sent to the controller.

        The comparison uses the two-decimal values kept as the last commanded
        pair, and the azimuth keeps the representation the tracker produced; a
        jump across the 0/360 boundary is sent rather than reinterpreted. The
        caller holds the lock.
        """

        if self.tracking_threshold_deg <= 0.0:
            return True
        if (
            self._last_commanded_azimuth_deg is None
            or self._last_commanded_elevation_deg is None
        ):
            return True
        target_azimuth = round(azimuth_deg, TARGET_DECIMALS)
        target_elevation = round(elevation_deg, TARGET_DECIMALS)
        azimuth_delta = abs(target_azimuth - self._last_commanded_azimuth_deg)
        if azimuth_delta > AZIMUTH_REPRESENTATION_LIMIT_DEG:
            return True
        elevation_delta = abs(target_elevation - self._last_commanded_elevation_deg)
        return (
            self._reaches_threshold(azimuth_delta)
            or self._reaches_threshold(elevation_delta)
        )

    def _reaches_threshold(self, delta_deg: float) -> bool:
        """Compares one axis change against the update threshold."""

        threshold = self.tracking_threshold_deg
        if threshold <= 0.0:
            return True
        return delta_deg >= threshold or math.isclose(
            delta_deg,
            threshold,
            rel_tol=0.0,
            abs_tol=THRESHOLD_TOLERANCE_DEG,
        )

    def _refresh_tracking_status(self, eligible: bool) -> None:
        """Recomputes the operator-facing tracking status. Caller holds the lock."""

        if not self.enabled:
            status, message = "disabled", "Rotator control is off"
        elif not self._tracking_active:
            status, message = "paused", None
        elif not self._snapshot.pass_active:
            status, message = "idle", None
        elif eligible:
            if self._holding_target and self.tracking_threshold_deg > 0.0:
                status = "holding_target"
                message = (
                    "Tracking active - holding target; change below "
                    f"{format_degrees(self.tracking_threshold_deg)} deg update threshold"
                )
            else:
                status, message = "tracking", None
        else:
            cutoff = format_degrees(self.min_elevation_deg)
            if not self._pass_had_eligible:
                status = "waiting_min_elevation"
                message = (
                    f"Tracking active - waiting for minimum elevation ({cutoff} deg)"
                )
            elif self._cutoff_home_pending:
                status = "below_min_elevation_home_pending"
                message = (
                    f"Below minimum elevation ({cutoff} deg) - "
                    "home command failed, retrying"
                )
            elif self._cutoff_home_latched:
                status = "below_min_elevation_home_commanded"
                message = f"Below minimum elevation ({cutoff} deg) - home commanded"
            else:
                status = "below_min_elevation"
                message = f"Below minimum elevation ({cutoff} deg) - not tracking"
        self._snapshot = _replace_snapshot(
            self._snapshot, tracking_status=status, tracking_message=message
        )

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
        with self._operation_lock:
            if self._shutting_down:
                return self.snapshot()
            # The pass check, the write, and the commit share one operation-lock
            # scope so a pass that becomes active while a manual caller waits
            # cannot be overridden by that manual move.
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
                was_connected = self._snapshot.connected
                commanded_azimuth = round(azimuth_deg, TARGET_DECIMALS)
                commanded_elevation = round(elevation_deg, TARGET_DECIMALS)
                # A manual move or home takes the operator out of any pending
                # cutoff home, and the next tracking target is forced out.
                self._last_commanded_azimuth_deg = None
                self._last_commanded_elevation_deg = None
                self._holding_target = False
                self._cutoff_home_pending = False
                self._cutoff_home_latched = True
                self._snapshot = _replace_snapshot(
                    self._snapshot,
                    connected=True,
                    pass_active=False,
                    manual_controls_enabled=True,
                    state_label="NO ACTIVE PASS, manual mode enabled",
                    target_azimuth_deg=commanded_azimuth,
                    target_elevation_deg=commanded_elevation,
                    last_write_at_utc=_utc_now(),
                    error=None,
                )
                self._consecutive_failures = 0
                self._refresh_tracking_status(False)
        if not was_connected:
            LOGGER.info("Rotator manual connection restored")
        return self.snapshot()

    def send_home(self) -> RotatorSnapshot:
        return self.manual_position(self.home_azimuth_deg, self.home_elevation_deg)

    def stop(self) -> RotatorSnapshot:
        if not self.enabled:
            return self.snapshot()
        with self._operation_lock:
            if self._shutting_down:
                return self.snapshot()
            try:
                self.client.stop()
            except Exception as exc:
                self._set_error(str(exc))
                return self.snapshot()
            with self._lock:
                was_connected = self._snapshot.connected
                # After a stop the controller is wherever it halted, so the next
                # eligible target is sent regardless of the update threshold.
                self._last_commanded_azimuth_deg = None
                self._last_commanded_elevation_deg = None
                self._holding_target = False
                self._snapshot = _replace_snapshot(
                    self._snapshot,
                    connected=True,
                    error=None,
                )
                self._consecutive_failures = 0
        if not was_connected:
            LOGGER.info("Rotator stop connection restored")
        return self.snapshot()

    def shutdown(self) -> None:
        """Stops and closes the client once, after any in-flight operation finishes."""

        errors: list[str] = []
        with self._operation_lock:
            if self._shutting_down:
                return
            self._shutting_down = True
            try:
                self.client.stop()
            except Exception as exc:
                errors.append(f"stop failed: {exc}")
            close = getattr(self.client, "close", None)
            if close is not None:
                try:
                    close()
                except Exception as exc:
                    errors.append(f"close failed: {exc}")
        if errors:
            raise RuntimeError("; ".join(errors))

    def _set_error(self, error: str) -> None:
        with self._lock:
            self._consecutive_failures += 1
            if self._consecutive_failures < self._failure_threshold:
                return
            previous_error = self._snapshot.error
            self._snapshot = _replace_snapshot(
                self._snapshot,
                connected=False,
                error=error,
            )
        if previous_error != error:
            LOGGER.warning("Rotator operation failed: %s", error)


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
        tracking_status="disabled",
        tracking_message="Rotator control is off",
    )


def _replace_snapshot(snapshot: RotatorSnapshot, **updates: Any) -> RotatorSnapshot:
    values = snapshot.__dict__.copy()
    values.update(updates)
    return RotatorSnapshot(**values)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()
