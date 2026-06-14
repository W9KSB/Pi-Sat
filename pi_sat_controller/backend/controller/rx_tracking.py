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
from typing import Any, Callable

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
from pi_sat_controller.backend.orbital.skyfield_engine import SkyfieldEngine
from pi_sat_controller.backend.rotator.rotator_manager import RotatorManager
from pi_sat_controller.backend.radio.radio_manager import RadioManager
from pi_sat_controller.backend.sdr.polling_sdr import PollingSdrManager


def is_rx_only_profile(transponder: TransponderProfile) -> bool:
    return transponder.type == "rx_only" or transponder.preferred_uplink <= 0


@dataclass(frozen=True)
class RxTrackingSnapshot:
    active: bool
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

    def _apply_current_plan(self, write_rx: bool) -> RxTrackingSnapshot:
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
        errors: list[str] = []
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
                active=current.active,
                norad_id=self.satellite.norad_id,
                satellite_name=current.satellite_name,
                transponder_name=current.transponder_name,
                azimuth_deg=current.azimuth_deg,
                elevation_deg=current.elevation_deg,
                latitude_deg=current.latitude_deg,
                longitude_deg=current.longitude_deg,
                range_km=current.range_km,
                range_rate_m_s=current.range_rate_m_s,
                downlink_center_hz=self.transponder.preferred_downlink,
                uplink_center_hz=None if self._rx_only else self.transponder.preferred_uplink,
                downlink_doppler_hz=current.downlink_doppler_hz,
                uplink_doppler_hz=None if self._rx_only else current.uplink_doppler_hz,
                user_downlink_offset_hz=user_downlink_offset,
                mapped_user_uplink_offset_hz=(
                    None if self._rx_only else plan.mapped_user_uplink_offset_hz
                ),
                sync_offsets=sync_offsets,
                target_rx_hz=plan.downlink_hz,
                calculated_tx_hz=None if self._rx_only else plan.uplink_hz,
                last_commanded_rx_hz=commanded_rx_hz,
                last_update_at_utc=_utc_now(),
                error=" | ".join(errors) if errors else None,
            )
            return self._last_snapshot

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

        pass_active = position.elevation_deg >= 0.0
        pass_transition: str | None = None
        downlink_doppler = doppler_shift_hz(
            self.transponder.preferred_downlink,
            position.range_rate_m_s,
        )
        uplink_doppler = None if self._rx_only else doppler_shift_hz(
            self.transponder.preferred_uplink,
            position.range_rate_m_s,
        )
        current_rx_hz = self.sdr_manager.snapshot().frequency_hz

        with self._lock:
            user_offset = self._user_downlink_offset_hz
            user_uplink_offset = self._user_uplink_offset_hz
            sync_offsets = False if self._rx_only else self._sync_offsets
            last_commanded = self._last_commanded_rx_hz
            last_commanded_tx_hz = self._last_commanded_tx_hz
            last_commanded_at = self._last_commanded_at

        if (
            write_devices
            and current_rx_hz is not None
            and last_commanded is not None
            and abs(current_rx_hz - last_commanded) > self.deadband_hz
            and monotonic() - last_commanded_at > 0.75
        ):
            rx_delta = current_rx_hz - last_commanded
            user_offset += rx_delta
            if sync_offsets and not self._rx_only:
                user_uplink_offset += map_downlink_offset_to_uplink(
                    rx_delta,
                    self.transponder,
                )
            with self._lock:
                self._user_downlink_offset_hz = user_offset
                self._user_uplink_offset_hz = user_uplink_offset
                self._last_commanded_rx_hz = current_rx_hz
                self._last_commanded_at = monotonic()
            last_commanded = current_rx_hz
            last_commanded_at = monotonic()

        plan = self._build_plan(
            user_offset,
            user_uplink_offset,
            downlink_doppler,
            uplink_doppler or 0,
        )

        commanded_rx_hz = self._last_commanded_rx_hz
        commanded_tx_hz = self._last_commanded_tx_hz
        errors: list[str] = []
        if write_devices and (
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
                position.azimuth_deg,
                position.elevation_deg,
            )
            if write_devices and pass_active:
                try:
                    self.rotator_manager.track_position(
                        position.azimuth_deg,
                        position.elevation_deg,
                    )
                except Exception as exc:
                    errors.append(str(exc))

        if (
            write_devices
            and not self._rx_only
            and plan.uplink_hz is not None
            and self.tx_radio_manager
            and (
                last_commanded_tx_hz is None
                or abs(last_commanded_tx_hz - plan.uplink_hz) > self.deadband_hz
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
                active=self._active,
                norad_id=self.satellite.norad_id,
                satellite_name=self.satellite.name,
                transponder_name=self.transponder.name,
                azimuth_deg=round(position.azimuth_deg, 2),
                elevation_deg=round(position.elevation_deg, 2),
                latitude_deg=round(position.latitude_deg, 2),
                longitude_deg=round(position.longitude_deg, 2),
                range_km=round(position.range_km, 1),
                range_rate_m_s=round(position.range_rate_m_s, 2),
                downlink_center_hz=self.transponder.preferred_downlink,
                uplink_center_hz=None if self._rx_only else self.transponder.preferred_uplink,
                downlink_doppler_hz=downlink_doppler,
                uplink_doppler_hz=uplink_doppler,
                user_downlink_offset_hz=user_offset,
                mapped_user_uplink_offset_hz=(
                    None if self._rx_only else plan.mapped_user_uplink_offset_hz
                ),
                sync_offsets=sync_offsets,
                target_rx_hz=plan.downlink_hz,
                calculated_tx_hz=None if self._rx_only else plan.uplink_hz,
                last_commanded_rx_hz=commanded_rx_hz,
                last_update_at_utc=_utc_now(),
                error=" | ".join(errors) if errors else None,
            )
            snapshot = self._last_snapshot

        if write_devices and pass_transition:
            self._emit_pass_transition(pass_transition, snapshot, position)

    def _ensure_rx_session_state(self, errors: list[str] | None = None) -> None:
        if self._rx_session_ready:
            return
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
            if errors is not None and vfo_state.error:
                errors.append(vfo_state.error)
        elif hasattr(self.sdr_manager, "set_vfo"):
            try:
                self.sdr_manager.set_vfo(
                    rx_target_vfo,
                    source="rx_tracking.session_setup",
                )
            except Exception as exc:
                if errors is not None:
                    errors.append(str(exc))
        if hasattr(self.sdr_manager, "try_set_mode"):
            mode_state = self.sdr_manager.try_set_mode(
                self.transponder.downlink_mode,
                source="rx_tracking.session_setup",
            )
            if errors is not None and mode_state.error:
                errors.append(mode_state.error)
        elif hasattr(self.sdr_manager, "set_mode"):
            try:
                self.sdr_manager.set_mode(
                    self.transponder.downlink_mode,
                    source="rx_tracking.session_setup",
                )
            except Exception as exc:
                if errors is not None:
                    errors.append(str(exc))
        self._rx_session_ready = True

    def _ensure_tx_session_state(self, errors: list[str] | None = None) -> None:
        if self._tx_session_ready or self.tx_radio_manager is None:
            return
        if not getattr(self.tx_radio_manager, "restore_vfo_after_write", None):
            vfo_state = self.tx_radio_manager.try_set_vfo(
                self.tx_radio_manager.target_vfo,
                source="rx_tracking.session_setup",
            )
            if errors is not None and vfo_state.error:
                errors.append(vfo_state.error)
        mode_state = self.tx_radio_manager.try_set_mode(
            self.transponder.uplink_mode,
            source="rx_tracking.session_setup",
        )
        if errors is not None and mode_state.error:
            errors.append(mode_state.error)
        self._tx_session_ready = True

    def _record_error(self, error: str) -> None:
        with self._lock:
            current = self._last_snapshot
            self._last_snapshot = RxTrackingSnapshot(
                active=self._active,
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
                sync_offsets=False if self._rx_only else self._sync_offsets,
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
