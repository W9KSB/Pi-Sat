from __future__ import annotations

"""RX/TX tracking control loop for one selected satellite transponder.

The manager combines orbital position, Doppler calculation, user offsets, SDR
control, optional TX radio control, and optional rotator coordination. Manual
offsets remain stable user intent while Doppler is recalculated each cycle.
"""

from dataclasses import dataclass, replace
from datetime import datetime, timezone
from threading import Event, Lock, Thread
from time import monotonic
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, Callable

from pi_sat_controller.backend.controller.frequency_planner import (
    plan_from_offsets,
    map_downlink_offset_to_uplink,
    map_uplink_offset_to_downlink,
)
from pi_sat_controller.backend.models import (
    FrequencyPlan,
    SatelliteProfile,
    TransponderProfile,
)
from pi_sat_controller.backend.orbital.doppler import doppler_shift_hz
from pi_sat_controller.backend.orbital.orbital_engine import SatellitePosition
from pi_sat_controller.backend.rotator.rotator_manager import RotatorManager
from pi_sat_controller.backend.radio.radio_manager import RadioManager
from pi_sat_controller.backend.sdr.polling_sdr import PollingSdrManager

if TYPE_CHECKING:
    from pi_sat_controller.backend.orbital.skyfield_engine import SkyfieldEngine

MAX_MANUAL_READBACK_DELTA_HZ = 2_000_000


def is_rx_only_profile(transponder: TransponderProfile) -> bool:
    return transponder.type == "rx_only" or transponder.preferred_uplink <= 0


@dataclass(frozen=True)
class RxTrackingSnapshot:
    active: bool
    pass_active: bool
    norad_id: int | None
    satellite_name: str | None
    transponder_name: str | None
    azimuth_deg: float | None
    elevation_deg: float | None
    latitude_deg: float | None
    longitude_deg: float | None
    range_km: float | None
    range_rate_m_s: float | None
    downlink_center_hz: int | None
    uplink_center_hz: int | None
    downlink_doppler_hz: int | None
    uplink_doppler_hz: int | None
    user_downlink_offset_hz: int
    mapped_user_uplink_offset_hz: int | None
    sync_offsets: bool
    target_rx_hz: int | None
    calculated_tx_hz: int | None
    last_commanded_rx_hz: int | None
    last_update_at_utc: str | None
    error: str | None

    def to_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


@dataclass(frozen=True)
class _ReadbackDelta:
    frequency_hz: int | None
    delta_hz: int | None
    ignored_error: str | None = None
    read_failed: bool = False


class RxTrackingManager:
    """Runs the live tuning loop for the currently selected satellite/profile."""

    def __init__(
        self,
        orbital_engine: SkyfieldEngine,
        sdr_manager: PollingSdrManager,
        satellite: SatelliteProfile,
        transponder: TransponderProfile,
        deadband_hz: int,
        rotator_manager: RotatorManager | None = None,
        tx_radio_manager: RadioManager | None = None,
        on_pass_start: Callable[[dict[str, Any]], None] | None = None,
        on_pass_end: Callable[[dict[str, Any]], None] | None = None,
        interval_s: float = 1.0,
    ) -> None:
        self.orbital_engine = orbital_engine
        self.sdr_manager = sdr_manager
        self.satellite = satellite
        self.transponder = transponder
        self.deadband_hz = deadband_hz
        self.rotator_manager = rotator_manager
        self.tx_radio_manager = tx_radio_manager
        self.on_pass_start = on_pass_start
        self.on_pass_end = on_pass_end
        self.interval_s = interval_s
        self._stop = Event()
        self._lock = Lock()
        self._thread: Thread | None = None
        self._active = False
        self._user_downlink_offset_hz = 0
        self._user_uplink_offset_hz = 0
        self._sync_offsets = True
        self._rx_only = is_rx_only_profile(transponder)
        self._rx_session_ready = False
        self._tx_session_ready = False
        self._last_pass_active = False
        self._last_commanded_rx_hz: int | None = None
        self._last_commanded_tx_hz: int | None = None
        self._last_commanded_at = 0.0
        self._last_snapshot = RxTrackingSnapshot(
            active=False,
            pass_active=False,
            norad_id=satellite.norad_id,
            satellite_name=satellite.name,
            transponder_name=transponder.name,
            azimuth_deg=None,
            elevation_deg=None,
            latitude_deg=None,
            longitude_deg=None,
            range_km=None,
            range_rate_m_s=None,
            downlink_center_hz=transponder.preferred_downlink,
            uplink_center_hz=None if self._rx_only else transponder.preferred_uplink,
            downlink_doppler_hz=None,
            uplink_doppler_hz=None,
            user_downlink_offset_hz=0,
            mapped_user_uplink_offset_hz=None,
            sync_offsets=True,
            target_rx_hz=None,
            calculated_tx_hz=None,
            last_commanded_rx_hz=None,
            last_update_at_utc=None,
            error=None,
        )

    def start(self) -> None:
        with self._lock:
            self._active = True
        self.refresh_snapshot_only()
        if self._thread is None:
            self._thread = Thread(target=self._run, name="rx-tracker", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        with self._lock:
            self._active = False
            self._last_snapshot = replace(self._last_snapshot, active=False)

    def shutdown(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None

    def update_runtime_dependencies(
        self,
        *,
        sdr_manager: PollingSdrManager,
        tx_radio_manager: RadioManager | None,
        rotator_manager: RotatorManager | None,
    ) -> None:
        """Swaps runtime device managers without resetting tracking state."""

        with self._lock:
            self.sdr_manager = sdr_manager
            self.tx_radio_manager = tx_radio_manager
            self.rotator_manager = rotator_manager
            self._rx_session_ready = False
            self._tx_session_ready = False

    def snapshot(self) -> RxTrackingSnapshot:
        with self._lock:
            return self._last_snapshot

    def reset_offset(self) -> RxTrackingSnapshot:
        with self._lock:
            self._user_downlink_offset_hz = 0
            self._user_uplink_offset_hz = 0
        return self._apply_current_plan(write_rx=False)

    def set_offset_sync(self, enabled: bool) -> RxTrackingSnapshot:
        with self._lock:
            sync_already_enabled = bool(self._sync_offsets)
        if enabled and not self._rx_only and not sync_already_enabled:
            baseline_error = self._capture_offset_sync_baseline()
            if baseline_error is not None:
                with self._lock:
                    self._sync_offsets = False
                return self._apply_current_plan(
                    write_rx=False,
                    write_tx=False,
                    initial_errors=[baseline_error],
                )
        with self._lock:
            self._sync_offsets = False if self._rx_only else enabled
        return self._apply_current_plan(write_rx=False)

    def adjust_downlink_offset(self, delta_hz: int) -> RxTrackingSnapshot:
        with self._lock:
            self._user_downlink_offset_hz += delta_hz
            if self._sync_offsets and not self._rx_only:
                self._user_uplink_offset_hz += map_downlink_offset_to_uplink(
                    delta_hz,
                    self.transponder,
                )
        return self._apply_current_plan(write_rx=False)

    def adjust_uplink_offset(self, delta_hz: int) -> RxTrackingSnapshot:
        if self._rx_only:
            return self._apply_current_plan(write_rx=False)
        with self._lock:
            self._user_uplink_offset_hz += delta_hz
            sync_offsets = self._sync_offsets
            if sync_offsets:
                self._user_downlink_offset_hz += map_uplink_offset_to_downlink(
                    delta_hz,
                    self.transponder,
                )
        return self._apply_current_plan(write_rx=False)

    def _apply_current_plan(
        self,
        write_rx: bool,
        *,
        write_tx: bool = True,
        initial_errors: list[str] | None = None,
    ) -> RxTrackingSnapshot:
        """Builds the current RX/TX plan and optionally writes it to hardware."""

        with self._lock:
            current = self._last_snapshot
            user_downlink_offset = self._user_downlink_offset_hz
            user_uplink_offset = self._user_uplink_offset_hz
            sync_offsets = False if self._rx_only else self._sync_offsets

        plan = self._build_plan(
            user_downlink_offset,
            user_uplink_offset,
            current.downlink_doppler_hz or 0,
            current.uplink_doppler_hz or 0,
        )

        commanded_rx_hz = self._last_commanded_rx_hz
        commanded_tx_hz = self._last_commanded_tx_hz
        errors: list[str] = list(initial_errors or [])
        if write_rx:
            self._ensure_rx_session_state(errors)
            sdr_state = self.sdr_manager.try_set_frequency(plan.downlink_hz)
            if sdr_state.error:
                errors.append(sdr_state.error)
            else:
                commanded_rx_hz = plan.downlink_hz
                with self._lock:
                    self._last_commanded_rx_hz = plan.downlink_hz
                    self._last_commanded_at = monotonic()

        if (
            not self._rx_only
            and write_tx
            and plan.uplink_hz is not None
            and self.tx_radio_manager
            and (
                commanded_tx_hz is None
                or abs(commanded_tx_hz - plan.uplink_hz) > self.deadband_hz
            )
        ):
            self._ensure_tx_session_state(errors)
            tx_state = self.tx_radio_manager.try_set_frequency(
                plan.uplink_hz,
                source="rx_tracking.apply_current_plan",
            )
            if tx_state.error:
                errors.append(tx_state.error)
            else:
                commanded_tx_hz = plan.uplink_hz
                with self._lock:
                    self._last_commanded_tx_hz = plan.uplink_hz

        with self._lock:
            self._last_snapshot = RxTrackingSnapshot(
                active=bool(current.active),
                pass_active=bool(current.pass_active),
                norad_id=self.satellite.norad_id,
                satellite_name=current.satellite_name,
                transponder_name=current.transponder_name,
                azimuth_deg=current.azimuth_deg,
                elevation_deg=current.elevation_deg,
                latitude_deg=current.latitude_deg,
                longitude_deg=current.longitude_deg,
                range_km=current.range_km,
                range_rate_m_s=current.range_rate_m_s,
                downlink_center_hz=_python_int(self.transponder.preferred_downlink),
                uplink_center_hz=(
                    None if self._rx_only else _python_int(self.transponder.preferred_uplink)
                ),
                downlink_doppler_hz=current.downlink_doppler_hz,
                uplink_doppler_hz=None if self._rx_only else current.uplink_doppler_hz,
                user_downlink_offset_hz=_python_int(user_downlink_offset),
                mapped_user_uplink_offset_hz=(
                    None if self._rx_only else _python_int(plan.mapped_user_uplink_offset_hz)
                ),
                sync_offsets=bool(sync_offsets),
                target_rx_hz=_python_int(plan.downlink_hz),
                calculated_tx_hz=None if self._rx_only else _python_int(plan.uplink_hz),
                last_commanded_rx_hz=None if commanded_rx_hz is None else _python_int(commanded_rx_hz),
                last_update_at_utc=_utc_now(),
                error=" | ".join(errors) if errors else None,
            )
            return self._last_snapshot

    def _capture_offset_sync_baseline(self) -> str | None:
        """Stores the current RX/TX hardware relationship as the sync baseline."""

        with self._lock:
            current = self._last_snapshot
            tracking_active = self._active
            pass_active = current.pass_active
            user_downlink_offset = self._user_downlink_offset_hz
            user_uplink_offset = self._user_uplink_offset_hz

        if not tracking_active or not pass_active:
            return None
        if self.tx_radio_manager is None:
            return "Cannot enable RX/TX offset sync: TX radio is unavailable."

        rx_snapshot = self._fresh_rx_snapshot()
        current_rx_hz = getattr(rx_snapshot, "frequency_hz", None)
        rx_error = getattr(rx_snapshot, "error", None)
        if rx_error:
            return f"Cannot enable RX/TX offset sync: RX readback failed: {rx_error}"
        if current_rx_hz is None:
            return "Cannot enable RX/TX offset sync: RX frequency unavailable."

        try:
            current_tx_hz = self.tx_radio_manager.get_frequency()
        except Exception as exc:
            return f"Cannot enable RX/TX offset sync: TX readback failed: {exc}"
        if current_tx_hz is None:
            return "Cannot enable RX/TX offset sync: TX frequency unavailable."

        downlink_doppler = current.downlink_doppler_hz or 0
        uplink_doppler = current.uplink_doppler_hz or 0
        next_user_downlink_offset = (
            current_rx_hz - self.transponder.preferred_downlink - downlink_doppler
        )
        next_user_uplink_offset = (
            current_tx_hz - self.transponder.preferred_uplink - uplink_doppler
        )

        downlink_delta = next_user_downlink_offset - user_downlink_offset
        uplink_delta = next_user_uplink_offset - user_uplink_offset
        if abs(downlink_delta) > MAX_MANUAL_READBACK_DELTA_HZ:
            return (
                "Cannot enable RX/TX offset sync: "
                f"RX readback delta {downlink_delta:+,} Hz is out-of-band."
            )
        if abs(uplink_delta) > MAX_MANUAL_READBACK_DELTA_HZ:
            return (
                "Cannot enable RX/TX offset sync: "
                f"TX readback delta {uplink_delta:+,} Hz is out-of-band."
            )

        with self._lock:
            self._user_downlink_offset_hz = next_user_downlink_offset
            self._user_uplink_offset_hz = next_user_uplink_offset
            self._last_commanded_rx_hz = current_rx_hz
            self._last_commanded_tx_hz = current_tx_hz
            self._last_commanded_at = monotonic()
        return None

    def _run(self) -> None:
        while not self._stop.is_set():
            with self._lock:
                active = self._active
            if active:
                self.update_once()
            if self._stop.wait(self.interval_s):
                break

    def update_once(self) -> None:
        try:
            position = self.orbital_engine.get_position(self.satellite.norad_id)
            self._apply_update(position, write_devices=True)
        except Exception as exc:
            self._record_error(str(exc))

    def refresh_snapshot_only(self) -> None:
        try:
            position = self.orbital_engine.get_position(self.satellite.norad_id)
            self._apply_update(position, write_devices=False)
        except Exception as exc:
            self._record_error(str(exc))

    def _apply_update(self, position: SatellitePosition, write_devices: bool) -> None:
        """Applies one orbital position update to SDR, TX, and rotator state."""

        pass_active = bool(position.elevation_deg >= 0.0)
        pass_transition: str | None = None
        downlink_doppler = doppler_shift_hz(
            self.transponder.preferred_downlink,
            position.range_rate_m_s,
        )
        uplink_doppler = None if self._rx_only else doppler_shift_hz(
            self.transponder.preferred_uplink,
            position.range_rate_m_s,
        )

        with self._lock:
            user_offset = self._user_downlink_offset_hz
            user_uplink_offset = self._user_uplink_offset_hz
            sync_offsets = False if self._rx_only else self._sync_offsets
            tracking_active = self._active
            last_commanded = self._last_commanded_rx_hz
            last_commanded_tx_hz = self._last_commanded_tx_hz

        current_rx_hz = self.sdr_manager.snapshot().frequency_hz
        current_tx_hz: int | None = None
        readback_errors: list[str] = []

        plan = self._build_plan(
            user_offset,
            user_uplink_offset,
            downlink_doppler,
            uplink_doppler or 0,
        )

        commanded_rx_hz = self._last_commanded_rx_hz
        commanded_tx_hz = self._last_commanded_tx_hz
        errors: list[str] = []
        skip_rx_write = False
        skip_tx_write = False
        if write_devices:
            rx_readback = self._read_rx_frequency_for_reconciliation(
                tracking_active,
                pass_active,
                last_commanded,
            )
            current_rx_hz = rx_readback.frequency_hz
            if rx_readback.ignored_error:
                readback_errors.append(rx_readback.ignored_error)
            skip_rx_write = rx_readback.read_failed

            tx_readback = _ReadbackDelta(frequency_hz=None, delta_hz=None)
            if not self._rx_only and self.tx_radio_manager and plan.uplink_hz is not None:
                tx_readback = self._read_tx_frequency_for_reconciliation(
                    tracking_active,
                    pass_active,
                    last_commanded_tx_hz,
                )
                current_tx_hz = tx_readback.frequency_hz
                if tx_readback.ignored_error:
                    readback_errors.append(tx_readback.ignored_error)
                skip_tx_write = tx_readback.read_failed

            rx_delta = rx_readback.delta_hz
            tx_delta = tx_readback.delta_hz
            if rx_delta is not None or tx_delta is not None:
                if rx_delta is not None and tx_delta is not None and sync_offsets:
                    sync_offsets = False
                    readback_errors.append(
                        "RX and TX both changed externally; offset sync disabled."
                    )
                if rx_delta is not None:
                    user_offset += rx_delta
                    if sync_offsets and not self._rx_only:
                        user_uplink_offset += map_downlink_offset_to_uplink(
                            rx_delta,
                            self.transponder,
                        )
                if tx_delta is not None and not self._rx_only:
                    user_uplink_offset += tx_delta
                    if sync_offsets:
                        user_offset += map_uplink_offset_to_downlink(
                            tx_delta,
                            self.transponder,
                        )
                with self._lock:
                    self._user_downlink_offset_hz = user_offset
                    self._user_uplink_offset_hz = user_uplink_offset
                    self._sync_offsets = False if self._rx_only else sync_offsets
                    if rx_delta is not None:
                        self._last_commanded_rx_hz = current_rx_hz
                        self._last_commanded_at = monotonic()
                    if tx_delta is not None:
                        self._last_commanded_tx_hz = current_tx_hz
                last_commanded = current_rx_hz if rx_delta is not None else last_commanded
                last_commanded_tx_hz = (
                    current_tx_hz if tx_delta is not None else last_commanded_tx_hz
                )
                commanded_rx_hz = current_rx_hz if rx_delta is not None else commanded_rx_hz
                commanded_tx_hz = (
                    current_tx_hz if tx_delta is not None else commanded_tx_hz
                )
                plan = self._build_plan(
                    user_offset,
                    user_uplink_offset,
                    downlink_doppler,
                    uplink_doppler or 0,
                )

        errors.extend(readback_errors)
        if write_devices and not skip_rx_write and (
            current_rx_hz is None or abs(current_rx_hz - plan.downlink_hz) > self.deadband_hz
        ):
            self._ensure_rx_session_state(errors)
            sdr_state = self.sdr_manager.try_set_frequency(plan.downlink_hz)
            if sdr_state.error:
                errors.append(sdr_state.error)
            else:
                commanded_rx_hz = plan.downlink_hz
                with self._lock:
                    self._last_commanded_rx_hz = plan.downlink_hz
                    self._last_commanded_at = monotonic()

        if self.rotator_manager is not None:
            self.rotator_manager.set_pass_active(
                pass_active,
                _python_float(position.azimuth_deg),
                _python_float(position.elevation_deg),
            )
            if write_devices and pass_active:
                try:
                    self.rotator_manager.track_position(
                        _python_float(position.azimuth_deg),
                        _python_float(position.elevation_deg),
                    )
                except Exception as exc:
                    errors.append(str(exc))

        if (
            write_devices
            and not skip_tx_write
            and not self._rx_only
            and plan.uplink_hz is not None
            and self.tx_radio_manager
            and (
                current_tx_hz is None
                or abs(current_tx_hz - plan.uplink_hz) > self.deadband_hz
            )
        ):
            self._ensure_tx_session_state(errors)
            tx_state = self.tx_radio_manager.try_set_frequency(
                plan.uplink_hz,
                source="rx_tracking.update_once",
            )
            if tx_state.error:
                errors.append(tx_state.error)
            else:
                commanded_tx_hz = plan.uplink_hz
                with self._lock:
                    self._last_commanded_tx_hz = plan.uplink_hz

        with self._lock:
            if write_devices:
                if pass_active and not self._last_pass_active:
                    pass_transition = "aos"
                elif not pass_active and self._last_pass_active:
                    pass_transition = "los"
                self._last_pass_active = pass_active
            self._last_snapshot = RxTrackingSnapshot(
                active=bool(self._active),
                pass_active=pass_active,
                norad_id=self.satellite.norad_id,
                satellite_name=self.satellite.name,
                transponder_name=self.transponder.name,
                azimuth_deg=round(_python_float(position.azimuth_deg), 2),
                elevation_deg=round(_python_float(position.elevation_deg), 2),
                latitude_deg=round(_python_float(position.latitude_deg), 2),
                longitude_deg=round(_python_float(position.longitude_deg), 2),
                range_km=round(_python_float(position.range_km), 1),
                range_rate_m_s=round(_python_float(position.range_rate_m_s), 2),
                downlink_center_hz=_python_int(self.transponder.preferred_downlink),
                uplink_center_hz=(
                    None if self._rx_only else _python_int(self.transponder.preferred_uplink)
                ),
                downlink_doppler_hz=_python_int(downlink_doppler),
                uplink_doppler_hz=None if uplink_doppler is None else _python_int(uplink_doppler),
                user_downlink_offset_hz=_python_int(user_offset),
                mapped_user_uplink_offset_hz=(
                    None if self._rx_only else _python_int(plan.mapped_user_uplink_offset_hz)
                ),
                sync_offsets=bool(sync_offsets),
                target_rx_hz=_python_int(plan.downlink_hz),
                calculated_tx_hz=None if self._rx_only else _python_int(plan.uplink_hz),
                last_commanded_rx_hz=None if commanded_rx_hz is None else _python_int(commanded_rx_hz),
                last_update_at_utc=_utc_now(),
                error=" | ".join(errors) if errors else None,
            )
            snapshot = self._last_snapshot

        if write_devices and pass_transition:
            self._emit_pass_transition(pass_transition, snapshot, position)

    def _read_rx_frequency_for_reconciliation(
        self,
        tracking_active: bool,
        pass_active: bool,
        last_commanded_hz: int | None,
    ) -> _ReadbackDelta:
        snapshot = self._fresh_rx_snapshot()
        current_hz = getattr(snapshot, "frequency_hz", None)
        error = getattr(snapshot, "error", None)
        if error:
            return _ReadbackDelta(
                frequency_hz=current_hz,
                delta_hz=None,
                ignored_error=f"Skipped RX manual readback check: {error}",
                read_failed=True,
            )
        if (
            current_hz is None
            or not tracking_active
            or not pass_active
            or last_commanded_hz is None
            or abs(current_hz - last_commanded_hz) <= self.deadband_hz
        ):
            return _ReadbackDelta(frequency_hz=current_hz, delta_hz=None)

        delta_hz = current_hz - last_commanded_hz
        if abs(delta_hz) > MAX_MANUAL_READBACK_DELTA_HZ:
            return _ReadbackDelta(
                frequency_hz=current_hz,
                delta_hz=None,
                ignored_error=(
                    f"Ignored RX readback delta {delta_hz:+,} Hz as out-of-band."
                ),
            )
        return _ReadbackDelta(frequency_hz=current_hz, delta_hz=delta_hz)

    def _read_tx_frequency_for_reconciliation(
        self,
        tracking_active: bool,
        pass_active: bool,
        last_commanded_hz: int | None,
    ) -> _ReadbackDelta:
        if self.tx_radio_manager is None:
            return _ReadbackDelta(frequency_hz=None, delta_hz=None)
        try:
            current_hz = self.tx_radio_manager.get_frequency()
        except Exception as exc:
            return _ReadbackDelta(
                frequency_hz=None,
                delta_hz=None,
                ignored_error=f"Skipped TX manual readback check: {exc}",
                read_failed=True,
            )
        if (
            current_hz is None
            or not tracking_active
            or not pass_active
            or last_commanded_hz is None
            or abs(current_hz - last_commanded_hz) <= self.deadband_hz
        ):
            return _ReadbackDelta(frequency_hz=current_hz, delta_hz=None)

        delta_hz = current_hz - last_commanded_hz
        if abs(delta_hz) > MAX_MANUAL_READBACK_DELTA_HZ:
            return _ReadbackDelta(
                frequency_hz=current_hz,
                delta_hz=None,
                ignored_error=(
                    f"Ignored TX readback delta {delta_hz:+,} Hz as out-of-band."
                ),
            )
        return _ReadbackDelta(frequency_hz=current_hz, delta_hz=delta_hz)

    def _fresh_rx_snapshot(self) -> Any:
        if hasattr(self.sdr_manager, "read_frequency_once"):
            try:
                snapshot = self.sdr_manager.read_frequency_once()
            except Exception as exc:
                return SimpleNamespace(frequency_hz=None, error=str(exc))
            if snapshot is not None:
                return snapshot
        if hasattr(self.sdr_manager, "poll_once"):
            snapshot = self.sdr_manager.poll_once()
            if snapshot is not None:
                return snapshot
        return self.sdr_manager.snapshot()

    def _ensure_rx_session_state(self, errors: list[str] | None = None) -> None:
        if self._rx_session_ready:
            return
        session_errors: list[str] = []
        rx_target_vfo = getattr(
            getattr(self.sdr_manager, "radio_manager", None),
            "target_vfo",
            None,
        )
        if hasattr(self.sdr_manager, "try_set_vfo"):
            vfo_state = self.sdr_manager.try_set_vfo(
                rx_target_vfo,
                source="rx_tracking.session_setup",
            )
            if vfo_state.error:
                session_errors.append(vfo_state.error)
        elif hasattr(self.sdr_manager, "set_vfo"):
            try:
                self.sdr_manager.set_vfo(
                    rx_target_vfo,
                    source="rx_tracking.session_setup",
                )
            except Exception as exc:
                session_errors.append(str(exc))
        if hasattr(self.sdr_manager, "try_set_mode"):
            mode_state = self.sdr_manager.try_set_mode(
                self.transponder.downlink_mode,
                source="rx_tracking.session_setup",
            )
            if mode_state.error:
                session_errors.append(mode_state.error)
        elif hasattr(self.sdr_manager, "set_mode"):
            try:
                self.sdr_manager.set_mode(
                    self.transponder.downlink_mode,
                    source="rx_tracking.session_setup",
                )
            except Exception as exc:
                session_errors.append(str(exc))
        if errors is not None and session_errors:
            errors.extend(session_errors)
        if session_errors:
            return
        self._rx_session_ready = True

    def _ensure_tx_session_state(self, errors: list[str] | None = None) -> None:
        if self._tx_session_ready or self.tx_radio_manager is None:
            return
        session_errors: list[str] = []
        try:
            if getattr(self.tx_radio_manager, "split_mode_vfo", None):
                self.tx_radio_manager.set_split_mode_enabled(
                    self.tx_radio_manager.split_mode_vfo,
                    source="rx_tracking.session_setup",
                )
            elif not getattr(self.tx_radio_manager, "restore_vfo_after_write", None):
                self.tx_radio_manager.set_vfo(
                    self.tx_radio_manager.target_vfo,
                    source="rx_tracking.session_setup",
                )
            self.tx_radio_manager.set_mode(
                self.transponder.uplink_mode,
                source="rx_tracking.session_setup",
            )
        except Exception as exc:
            session_errors.append(str(exc))
        if errors is not None and session_errors:
            errors.extend(session_errors)
        if session_errors:
            return
        self._tx_session_ready = True

    def _record_error(self, error: str) -> None:
        with self._lock:
            current = self._last_snapshot
            self._last_snapshot = RxTrackingSnapshot(
                active=bool(self._active),
                pass_active=bool(current.pass_active),
                norad_id=self.satellite.norad_id,
                satellite_name=current.satellite_name,
                transponder_name=current.transponder_name,
                azimuth_deg=current.azimuth_deg,
                elevation_deg=current.elevation_deg,
                latitude_deg=current.latitude_deg,
                longitude_deg=current.longitude_deg,
                range_km=current.range_km,
                range_rate_m_s=current.range_rate_m_s,
                downlink_center_hz=current.downlink_center_hz,
                uplink_center_hz=None if self._rx_only else current.uplink_center_hz,
                downlink_doppler_hz=current.downlink_doppler_hz,
                uplink_doppler_hz=None if self._rx_only else current.uplink_doppler_hz,
                user_downlink_offset_hz=self._user_downlink_offset_hz,
                mapped_user_uplink_offset_hz=(
                    None if self._rx_only else current.mapped_user_uplink_offset_hz
                ),
                sync_offsets=bool(False if self._rx_only else self._sync_offsets),
                target_rx_hz=current.target_rx_hz,
                calculated_tx_hz=None if self._rx_only else current.calculated_tx_hz,
                last_commanded_rx_hz=self._last_commanded_rx_hz,
                last_update_at_utc=_utc_now(),
                error=error,
            )

    def _emit_pass_transition(
        self,
        event_name: str,
        snapshot: RxTrackingSnapshot,
        position: SatellitePosition,
    ) -> None:
        callback = self.on_pass_start if event_name == "aos" else self.on_pass_end
        if callback is None:
            return
        callback(
            {
                "event": event_name.upper(),
                "norad_id": self.satellite.norad_id,
                "satellite_name": self.satellite.name,
                "transponder_name": self.transponder.name,
                "azimuth_deg": round(position.azimuth_deg, 2),
                "elevation_deg": round(position.elevation_deg, 2),
                "latitude_deg": round(position.latitude_deg, 5),
                "longitude_deg": round(position.longitude_deg, 5),
                "range_km": round(position.range_km, 3),
                "range_rate_m_s": round(position.range_rate_m_s, 3),
                "target_rx_hz": snapshot.target_rx_hz,
                "target_tx_hz": snapshot.calculated_tx_hz,
            }
        )

    def _build_plan(
        self,
        user_downlink_offset_hz: int,
        user_uplink_offset_hz: int,
        downlink_doppler_hz: int,
        uplink_doppler_hz: int,
    ):
        if self._rx_only:
            return FrequencyPlan(
                downlink_hz=(
                    self.transponder.preferred_downlink
                    + downlink_doppler_hz
                    + user_downlink_offset_hz
                ),
                uplink_hz=None,
                user_downlink_offset_hz=user_downlink_offset_hz,
                mapped_user_uplink_offset_hz=None,
                downlink_doppler_hz=downlink_doppler_hz,
                uplink_doppler_hz=None,
            )
        return plan_from_offsets(
            transponder=self.transponder,
            user_downlink_offset_hz=user_downlink_offset_hz,
            user_uplink_offset_hz=user_uplink_offset_hz,
            downlink_doppler_hz=downlink_doppler_hz,
            uplink_doppler_hz=uplink_doppler_hz,
        )


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _python_float(value: Any) -> float:
    return float(value)


def _python_int(value: Any) -> int:
    return int(value)
