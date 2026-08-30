from __future__ import annotations

"""RX/TX tracking control loop for one selected satellite transponder.

The manager combines orbital position, Doppler calculation, user offsets, SDR
control, optional TX radio control, and optional rotator coordination. Manual
offsets remain stable user intent while Doppler is recalculated each cycle.
"""

from contextlib import nullcontext
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
from pi_sat_controller.backend.orbital.doppler import (
    doppler_shift_hz,
    uplink_doppler_correction_hz,
)
from pi_sat_controller.backend.orbital.orbital_engine import SatellitePosition
from pi_sat_controller.backend.rotator.rotator_manager import RotatorManager
from pi_sat_controller.backend.radio.radio_manager import (
    RadioManager,
    normalize_hamlib_mode,
)
from pi_sat_controller.backend.radio.radio_state import RadioStateClassification
from pi_sat_controller.backend.sdr.polling_sdr import PollingSdrManager

if TYPE_CHECKING:
    from pi_sat_controller.backend.orbital.skyfield_engine import SkyfieldEngine

MAX_MANUAL_READBACK_DELTA_HZ = 2_000_000
FM_SETUP_PASSBAND_HZ = 15_000


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
    virtual_rit_hz: int
    manual_offsets_enabled: bool
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
    observed_frequency_hz: int | None = None


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
        cat_rate_limit_hz: int = 5,
        manual_offset_readback_active_pass_only: bool = False,
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
        self.cat_rate_limit_hz = max(1, int(cat_rate_limit_hz))
        self.manual_offset_readback_active_pass_only = bool(
            manual_offset_readback_active_pass_only
        )
        self._stop = Event()
        self._wake = Event()
        self._lock = Lock()
        self._update_lock = Lock()
        self._thread: Thread | None = None
        self._active = False
        self._user_downlink_offset_hz = 0
        self._user_uplink_offset_hz = 0
        self._virtual_rit_hz = 0
        self._manual_offsets_enabled = True
        self._sync_offsets = True
        self._sync_enable_pending = False
        self._rx_only = is_rx_only_profile(transponder)
        self._rx_session_ready = False
        self._tx_session_ready = False
        self._rx_session_generation: int | None = None
        self._tx_session_generation: int | None = None
        self._last_pass_active = False
        self._last_commanded_rx_hz: int | None = None
        self._last_commanded_tx_hz: int | None = None
        self._last_observed_rx_hz: int | None = None
        self._last_observed_tx_hz: int | None = None
        self._last_commanded_at = 0.0
        self._last_rx_write_at = 0.0
        self._last_tx_write_at = 0.0
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
            virtual_rit_hz=0,
            manual_offsets_enabled=True,
            sync_offsets=True,
            target_rx_hz=None,
            calculated_tx_hz=None,
            last_commanded_rx_hz=None,
            last_update_at_utc=None,
            error=None,
        )

    def start(self) -> None:
        with self._lock:
            was_active = self._active
            self._active = True
            if not was_active:
                self._rx_session_ready = False
                self._tx_session_ready = False
        self._set_background_polling_enabled(False)
        self.refresh_snapshot_only()
        if self._thread is None:
            self._thread = Thread(target=self._run, name="rx-tracker", daemon=True)
            self._thread.start()
        else:
            self._wake.set()

    def stop(self) -> None:
        with self._lock:
            self._active = False
            self._last_snapshot = replace(self._last_snapshot, active=False)
        self._set_background_polling_enabled(True)
        self._wake.set()

    def shutdown(self) -> None:
        with self._lock:
            self._active = False
        self._set_background_polling_enabled(True)
        self._stop.set()
        self._wake.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None

    def update_runtime_dependencies(
        self,
        *,
        sdr_manager: PollingSdrManager,
        tx_radio_manager: RadioManager | None,
        rotator_manager: RotatorManager | None,
        manual_offset_readback_active_pass_only: bool | None = None,
    ) -> None:
        """Swaps runtime device managers without resetting tracking state."""

        with self._update_lock:
            with self._lock:
                self.sdr_manager = sdr_manager
                self.tx_radio_manager = tx_radio_manager
                self.rotator_manager = rotator_manager
                self._rx_session_ready = False
                self._tx_session_ready = False
                self._rx_session_generation = None
                self._tx_session_generation = None
                if manual_offset_readback_active_pass_only is not None:
                    self.manual_offset_readback_active_pass_only = bool(
                        manual_offset_readback_active_pass_only
                    )
                active = self._active
            self._set_background_polling_enabled(not active)

    def update_target(
        self,
        satellite: SatelliteProfile,
        transponder: TransponderProfile,
    ) -> None:
        """Retargets the live worker without destroying its thread or CAT runtime."""

        with self._update_lock:
            with self._lock:
                self.satellite = satellite
                self.transponder = transponder
                self._rx_only = is_rx_only_profile(transponder)
                self._user_downlink_offset_hz = 0
                self._user_uplink_offset_hz = 0
                self._virtual_rit_hz = 0
                self._sync_offsets = not self._rx_only
                self._sync_enable_pending = False
                self._rx_session_ready = False
                self._tx_session_ready = False
                self._last_pass_active = False
                self._last_commanded_rx_hz = None
                self._last_commanded_tx_hz = None
                self._last_observed_rx_hz = None
                self._last_observed_tx_hz = None
                self._last_rx_write_at = 0.0
                self._last_tx_write_at = 0.0
        self.refresh_snapshot_only()
        self._wake.set()

    def snapshot(self) -> RxTrackingSnapshot:
        with self._lock:
            return self._last_snapshot

    def reset_offset(self) -> RxTrackingSnapshot:
        with self._lock:
            self._user_downlink_offset_hz = 0
            self._user_uplink_offset_hz = 0
            self._virtual_rit_hz = 0
            self._sync_enable_pending = False
        snapshot = self._apply_current_plan(write_rx=False, write_tx=False)
        self._wake.set()
        return snapshot

    def reset_virtual_rit(self) -> RxTrackingSnapshot:
        with self._lock:
            self._virtual_rit_hz = 0
        snapshot = self._apply_current_plan(write_rx=False, write_tx=False)
        self._wake.set()
        return snapshot

    def set_manual_offsets_enabled(self, enabled: bool) -> RxTrackingSnapshot:
        with self._lock:
            self._manual_offsets_enabled = bool(enabled)
            if not enabled:
                self._user_downlink_offset_hz = 0
                self._user_uplink_offset_hz = 0
                self._virtual_rit_hz = 0
                self._sync_enable_pending = False
        snapshot = self._apply_current_plan(write_rx=False, write_tx=False)
        self._wake.set()
        return snapshot

    def set_offset_sync(self, enabled: bool) -> RxTrackingSnapshot:
        with self._lock:
            sync_already_enabled = bool(self._sync_offsets)
            sync_already_pending = bool(self._sync_enable_pending)
            self._sync_offsets = False if self._rx_only else enabled
            self._sync_enable_pending = bool(
                enabled
                and not self._rx_only
                and self._manual_offsets_enabled
                and (sync_already_pending or not sync_already_enabled)
            )
        snapshot = self._apply_current_plan(write_rx=False, write_tx=False)
        self._wake.set()
        return snapshot

    def adjust_downlink_offset(self, delta_hz: int) -> RxTrackingSnapshot:
        with self._lock:
            manual_offsets_enabled = self._manual_offsets_enabled
            if manual_offsets_enabled:
                self._user_downlink_offset_hz += delta_hz
                if self._sync_offsets and not self._rx_only:
                    self._user_uplink_offset_hz += map_downlink_offset_to_uplink(
                        delta_hz,
                        self.transponder,
                    )
        snapshot = self._apply_current_plan(write_rx=False, write_tx=False)
        self._wake.set()
        return snapshot

    def adjust_virtual_rit(self, delta_hz: int) -> RxTrackingSnapshot:
        with self._lock:
            if self._manual_offsets_enabled:
                self._virtual_rit_hz += delta_hz
        snapshot = self._apply_current_plan(write_rx=False, write_tx=False)
        self._wake.set()
        return snapshot

    def adjust_uplink_offset(self, delta_hz: int) -> RxTrackingSnapshot:
        if self._rx_only:
            snapshot = self._apply_current_plan(write_rx=False, write_tx=False)
            self._wake.set()
            return snapshot
        with self._lock:
            manual_offsets_enabled = self._manual_offsets_enabled
            if manual_offsets_enabled:
                self._user_uplink_offset_hz += delta_hz
                sync_offsets = self._sync_offsets
                if sync_offsets:
                    self._user_downlink_offset_hz += map_uplink_offset_to_downlink(
                        delta_hz,
                        self.transponder,
                    )
        snapshot = self._apply_current_plan(write_rx=False, write_tx=False)
        self._wake.set()
        return snapshot

    def _apply_current_plan(
        self,
        write_rx: bool,
        *,
        write_tx: bool = False,
        initial_errors: list[str] | None = None,
    ) -> RxTrackingSnapshot:
        """Builds the current RX/TX plan and optionally writes it to hardware."""

        with self._lock:
            current = self._last_snapshot
            user_downlink_offset = self._user_downlink_offset_hz
            user_uplink_offset = self._user_uplink_offset_hz
            virtual_rit_hz = self._virtual_rit_hz
            manual_offsets_enabled = self._manual_offsets_enabled
            sync_offsets = False if self._rx_only else self._sync_offsets

        plan = self._build_plan(
            user_downlink_offset,
            user_uplink_offset,
            virtual_rit_hz,
            current.downlink_doppler_hz or 0,
            current.uplink_doppler_hz or 0,
        )

        commanded_rx_hz = self._last_commanded_rx_hz
        commanded_tx_hz = self._last_commanded_tx_hz
        errors: list[str] = list(initial_errors or [])
        rx_frequency_written = False
        tx_frequency_written = False
        if write_rx:
            sdr_state = self.sdr_manager.try_set_frequency(plan.downlink_hz)
            if sdr_state.error:
                errors.append(sdr_state.error)
            else:
                commanded_rx_hz = plan.downlink_hz
                with self._lock:
                    self._last_commanded_rx_hz = plan.downlink_hz
                    self._last_commanded_at = monotonic()
                rx_frequency_written = True

        uses_split_mode = bool(
            self.tx_radio_manager is not None
            and getattr(self.tx_radio_manager, "split_mode_vfo", None)
        )
        if not self._rx_only and write_tx and uses_split_mode:
            self._ensure_tx_session_state(errors)

        if (
            not self._rx_only
            and write_tx
            and plan.uplink_hz is not None
            and self.tx_radio_manager
            and self._cat_write_due(self._last_tx_write_at)
            and (
                commanded_tx_hz is None
                or abs(commanded_tx_hz - plan.uplink_hz) > self.deadband_hz
            )
        ):
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
                    self._last_tx_write_at = monotonic()
                tx_frequency_written = True

        if rx_frequency_written:
            self._ensure_rx_session_state(errors)
        if tx_frequency_written and not uses_split_mode:
            self._ensure_tx_session_state(errors)

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
                virtual_rit_hz=_python_int(virtual_rit_hz),
                manual_offsets_enabled=bool(manual_offsets_enabled),
                sync_offsets=bool(sync_offsets),
                target_rx_hz=_python_int(plan.downlink_hz),
                calculated_tx_hz=None if self._rx_only else _python_int(plan.uplink_hz),
                last_commanded_rx_hz=None if commanded_rx_hz is None else _python_int(commanded_rx_hz),
                last_update_at_utc=_utc_now(),
                error=" | ".join(errors) if errors else None,
            )
            return self._last_snapshot

    def _run(self) -> None:
        next_update_at = 0.0
        while not self._stop.is_set():
            with self._lock:
                active = self._active

            if not active:
                next_update_at = 0.0
                self._wake.wait()
                self._wake.clear()
                continue

            now = monotonic()
            if now < next_update_at:
                self._wake.wait(next_update_at - now)
                self._wake.clear()
                continue

            self.update_once()
            next_update_at = monotonic() + self.interval_s
            self._wake.clear()

    def update_once(self) -> None:
        with self._update_lock:
            try:
                with self._radio_operation_batch():
                    position = self.orbital_engine.get_position(self.satellite.norad_id)
                    self._apply_update(position, write_devices=True)
            except Exception as exc:
                self._record_error(str(exc))

    def refresh_snapshot_only(self) -> None:
        with self._update_lock:
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
        uplink_doppler = None if self._rx_only else uplink_doppler_correction_hz(
            self.transponder.preferred_uplink,
            position.range_rate_m_s,
        )

        with self._lock:
            user_offset = self._user_downlink_offset_hz
            user_uplink_offset = self._user_uplink_offset_hz
            virtual_rit_hz = self._virtual_rit_hz
            manual_offsets_enabled = self._manual_offsets_enabled
            sync_offsets = False if self._rx_only else self._sync_offsets
            tracking_active = self._active
            sync_enable_pending = self._sync_enable_pending
            last_commanded = self._last_commanded_rx_hz
            last_commanded_tx_hz = self._last_commanded_tx_hz

        current_rx_hz = self.sdr_manager.snapshot().frequency_hz
        current_tx_hz: int | None = None
        readback_errors: list[str] = []

        plan = self._build_plan(
            user_offset,
            user_uplink_offset,
            virtual_rit_hz,
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
            if not rx_readback.read_failed and current_rx_hz is not None:
                with self._lock:
                    self._last_observed_rx_hz = (
                        rx_readback.observed_frequency_hz
                        if rx_readback.observed_frequency_hz is not None
                        else current_rx_hz
                    )

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
                if not tx_readback.read_failed and current_tx_hz is not None:
                    with self._lock:
                        self._last_observed_tx_hz = (
                            tx_readback.observed_frequency_hz
                            if tx_readback.observed_frequency_hz is not None
                            else current_tx_hz
                        )

            rx_delta = rx_readback.delta_hz
            tx_delta = tx_readback.delta_hz
            if sync_enable_pending and manual_offsets_enabled:
                baseline_error: str | None = None
                if rx_readback.read_failed or current_rx_hz is None:
                    baseline_error = "Waiting for RX readback to enable offset sync."
                elif self.tx_radio_manager is None:
                    baseline_error = (
                        "Cannot enable RX/TX offset sync: TX radio is unavailable."
                    )
                elif tx_readback.read_failed or current_tx_hz is None:
                    baseline_error = "Waiting for TX readback to enable offset sync."
                else:
                    next_user_downlink_offset = (
                        current_rx_hz
                        - self.transponder.preferred_downlink
                        - downlink_doppler
                        - virtual_rit_hz
                    )
                    next_user_uplink_offset = (
                        current_tx_hz
                        - self.transponder.preferred_uplink
                        - (uplink_doppler or 0)
                    )
                    downlink_delta = next_user_downlink_offset - user_offset
                    uplink_delta = next_user_uplink_offset - user_uplink_offset
                    if abs(downlink_delta) > MAX_MANUAL_READBACK_DELTA_HZ:
                        baseline_error = (
                            "Cannot enable RX/TX offset sync: "
                            f"RX readback delta {downlink_delta:+,} Hz is out-of-band."
                        )
                    elif abs(uplink_delta) > MAX_MANUAL_READBACK_DELTA_HZ:
                        baseline_error = (
                            "Cannot enable RX/TX offset sync: "
                            f"TX readback delta {uplink_delta:+,} Hz is out-of-band."
                        )
                    else:
                        user_offset = next_user_downlink_offset
                        user_uplink_offset = next_user_uplink_offset
                        commanded_rx_hz = current_rx_hz
                        commanded_tx_hz = current_tx_hz
                        last_commanded = current_rx_hz
                        last_commanded_tx_hz = current_tx_hz
                        with self._lock:
                            self._user_downlink_offset_hz = user_offset
                            self._user_uplink_offset_hz = user_uplink_offset
                            self._last_commanded_rx_hz = current_rx_hz
                            self._last_commanded_tx_hz = current_tx_hz
                            self._last_commanded_at = monotonic()
                            self._sync_enable_pending = False
                        plan = self._build_plan(
                            user_offset,
                            user_uplink_offset,
                            virtual_rit_hz,
                            downlink_doppler,
                            uplink_doppler or 0,
                        )
                if baseline_error is not None:
                    readback_errors.append(baseline_error)
                    skip_rx_write = True
                    skip_tx_write = True
                    if baseline_error.startswith("Cannot enable"):
                        sync_offsets = False
                        with self._lock:
                            self._sync_offsets = False
                            self._sync_enable_pending = False
            elif manual_offsets_enabled and (rx_delta is not None or tx_delta is not None):
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
                    virtual_rit_hz,
                    downlink_doppler,
                    uplink_doppler or 0,
                )

        errors.extend(readback_errors)
        rx_frequency_ready = bool(
            current_rx_hz is not None
            and abs(current_rx_hz - plan.downlink_hz) <= self.deadband_hz
        )

        if (
            write_devices
            and not skip_rx_write
            and self._cat_write_due(self._last_rx_write_at)
            and (
                current_rx_hz is None
                or abs(current_rx_hz - plan.downlink_hz) > self.deadband_hz
            )
        ):
            sdr_state = self.sdr_manager.try_set_frequency(plan.downlink_hz)
            if sdr_state.error:
                errors.append(sdr_state.error)
            else:
                commanded_rx_hz = plan.downlink_hz
                with self._lock:
                    self._last_commanded_rx_hz = plan.downlink_hz
                    self._last_commanded_at = monotonic()
                    self._last_rx_write_at = self._last_commanded_at
                rx_frequency_ready = True

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

        tx_frequency_ready = bool(
            plan.uplink_hz is not None
            and current_tx_hz is not None
            and abs(current_tx_hz - plan.uplink_hz) <= self.deadband_hz
        )
        tx_uses_split_mode = bool(
            self.tx_radio_manager is not None
            and getattr(self.tx_radio_manager, "split_mode_vfo", None)
        )
        if (
            write_devices
            and not self._rx_only
            and self.tx_radio_manager is not None
            and tx_uses_split_mode
        ):
            self._ensure_tx_session_state(errors)

        if (
            write_devices
            and not self._rx_only
            and not skip_tx_write
            and plan.uplink_hz is not None
            and self.tx_radio_manager
            and (
                current_tx_hz is None
                or abs(current_tx_hz - plan.uplink_hz) > self.deadband_hz
            )
            and self._cat_write_due(self._last_tx_write_at)
        ):
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
                    self._last_tx_write_at = monotonic()
                tx_frequency_ready = True

        if write_devices and rx_frequency_ready:
            self._ensure_rx_session_state(errors)

        if (
            write_devices
            and not self._rx_only
            and self.tx_radio_manager is not None
            and tx_frequency_ready
            and not tx_uses_split_mode
        ):
            self._ensure_tx_session_state(errors)

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
                virtual_rit_hz=_python_int(virtual_rit_hz),
                manual_offsets_enabled=bool(manual_offsets_enabled),
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
        observation_reader = getattr(
            self.sdr_manager,
            "read_frequency_for_reconciliation",
            None,
        )
        if observation_reader is not None:
            observation = observation_reader()
            if observation.error:
                return _ReadbackDelta(
                    frequency_hz=observation.frequency_hz,
                    delta_hz=None,
                    ignored_error=f"Skipped RX manual readback check: {observation.error}",
                    read_failed=True,
                    observed_frequency_hz=observation.frequency_hz,
                )
            if not observation.from_poll and observation.classification != RadioStateClassification.EXTERNAL_CHANGE:
                return _ReadbackDelta(
                    frequency_hz=(
                        last_commanded_hz
                        if last_commanded_hz is not None
                        else observation.frequency_hz
                    ),
                    delta_hz=None,
                    observed_frequency_hz=observation.frequency_hz,
                )
            current_hz = observation.frequency_hz
        else:
            snapshot = self._fresh_rx_snapshot()
            current_hz = getattr(snapshot, "frequency_hz", None)
            error = getattr(snapshot, "error", None)
            if error:
                return _ReadbackDelta(
                    frequency_hz=current_hz,
                    delta_hz=None,
                    ignored_error=f"Skipped RX manual readback check: {error}",
                    read_failed=True,
                    observed_frequency_hz=current_hz,
                )
        if (
            current_hz is None
            or not tracking_active
            or (
                self.manual_offset_readback_active_pass_only
                and not pass_active
            )
            or last_commanded_hz is None
            or abs(current_hz - last_commanded_hz) <= self.deadband_hz
        ):
            return _ReadbackDelta(
                frequency_hz=current_hz,
                delta_hz=None,
                observed_frequency_hz=current_hz,
            )

        delta_hz = current_hz - last_commanded_hz
        if abs(delta_hz) > MAX_MANUAL_READBACK_DELTA_HZ:
            return _ReadbackDelta(
                frequency_hz=current_hz,
                delta_hz=None,
                ignored_error=(
                    f"Ignored RX readback delta {delta_hz:+,} Hz as out-of-band."
                ),
                read_failed=True,
                observed_frequency_hz=current_hz,
            )
        return _ReadbackDelta(
            frequency_hz=current_hz,
            delta_hz=delta_hz,
            observed_frequency_hz=current_hz,
        )

    def _read_tx_frequency_for_reconciliation(
        self,
        tracking_active: bool,
        pass_active: bool,
        last_commanded_hz: int | None,
    ) -> _ReadbackDelta:
        if self.tx_radio_manager is None:
            return _ReadbackDelta(frequency_hz=None, delta_hz=None)
        observation_reader = getattr(
            self.tx_radio_manager,
            "get_frequency_for_reconciliation",
            None,
        )
        try:
            if observation_reader is not None:
                observation = observation_reader()
                if observation.error:
                    raise RuntimeError(observation.error)
                if not observation.from_poll and observation.classification != RadioStateClassification.EXTERNAL_CHANGE:
                    return _ReadbackDelta(
                        frequency_hz=(
                            last_commanded_hz
                            if last_commanded_hz is not None
                            else observation.frequency_hz
                        ),
                        delta_hz=None,
                        observed_frequency_hz=observation.frequency_hz,
                    )
                current_hz = observation.frequency_hz
            else:
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
            or (
                self.manual_offset_readback_active_pass_only
                and not pass_active
            )
            or last_commanded_hz is None
            or abs(current_hz - last_commanded_hz) <= self.deadband_hz
        ):
            return _ReadbackDelta(
                frequency_hz=current_hz,
                delta_hz=None,
                observed_frequency_hz=current_hz,
            )

        delta_hz = current_hz - last_commanded_hz
        if abs(delta_hz) > MAX_MANUAL_READBACK_DELTA_HZ:
            return _ReadbackDelta(
                frequency_hz=current_hz,
                delta_hz=None,
                ignored_error=(
                    f"Ignored TX readback delta {delta_hz:+,} Hz as out-of-band."
                ),
                read_failed=True,
                observed_frequency_hz=current_hz,
            )
        return _ReadbackDelta(
            frequency_hz=current_hz,
            delta_hz=delta_hz,
            observed_frequency_hz=current_hz,
        )

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

    def _set_background_polling_enabled(self, enabled: bool) -> None:
        setter = getattr(self.sdr_manager, "set_background_polling_enabled", None)
        if setter is not None:
            setter(enabled)

    def _radio_operation_batch(self):
        radio_manager = getattr(self.sdr_manager, "radio_manager", None)
        client = getattr(radio_manager, "client", None)
        operation_batch = getattr(client, "operation_batch", None)
        if operation_batch is None:
            return nullcontext()
        return operation_batch()

    def _ensure_rx_session_state(self, errors: list[str] | None = None) -> None:
        radio_manager = getattr(self.sdr_manager, "radio_manager", None)
        if radio_manager is not None and hasattr(radio_manager, "connection_generation"):
            generation = radio_manager.connection_generation()
            if self._rx_session_generation != generation:
                self._rx_session_ready = False
                self._rx_session_generation = generation
        if self._rx_session_ready:
            return
        # A transponder/start/reconnect setup is attempted once. A CAT error is
        # reported, but it must not turn the tracking loop into a mode writer.
        self._rx_session_ready = True
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
                force=True,
                passband_hz=_session_setup_passband_hz(
                    self.transponder.downlink_mode
                ),
            )
            if mode_state.error:
                session_errors.append(mode_state.error)
        elif hasattr(self.sdr_manager, "set_mode"):
            try:
                self.sdr_manager.set_mode(
                    self.transponder.downlink_mode,
                    source="rx_tracking.session_setup",
                    force=True,
                    passband_hz=_session_setup_passband_hz(
                        self.transponder.downlink_mode
                    ),
                )
            except Exception as exc:
                session_errors.append(str(exc))
        if errors is not None and session_errors:
            errors.extend(session_errors)

    def _ensure_tx_session_state(self, errors: list[str] | None = None) -> None:
        if self.tx_radio_manager is not None and hasattr(
            self.tx_radio_manager,
            "connection_generation",
        ):
            generation = self.tx_radio_manager.connection_generation()
            if self._tx_session_generation != generation:
                self._tx_session_ready = False
                self._tx_session_generation = generation
        if self._tx_session_ready or self.tx_radio_manager is None:
            return
        # See RX setup above: setup errors are surfaced once, not retried on
        # every Doppler update where they could overwrite operator changes.
        self._tx_session_ready = True
        session_errors: list[str] = []
        tone_errors: list[str] = []
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
                force=True,
                passband_hz=_session_setup_passband_hz(
                    self.transponder.uplink_mode
                ),
            )
        except Exception as exc:
            session_errors.append(str(exc))
        if hasattr(
            self.tx_radio_manager,
            "try_set_ctcss_tone",
        ):
            tone_state = self.tx_radio_manager.try_set_ctcss_tone(
                self.transponder.tone,
                source="rx_tracking.session_setup",
            )
            if tone_state.error:
                tone_errors.append(tone_state.error)
        if errors is not None:
            errors.extend(session_errors)
            errors.extend(tone_errors)

    def _cat_write_due(self, last_write_at: float) -> bool:
        return monotonic() - last_write_at >= (1.0 / self.cat_rate_limit_hz)

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
                virtual_rit_hz=_python_int(self._virtual_rit_hz),
                manual_offsets_enabled=bool(self._manual_offsets_enabled),
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
        virtual_rit_hz: int,
        downlink_doppler_hz: int,
        uplink_doppler_hz: int,
    ):
        if self._rx_only:
            return FrequencyPlan(
                downlink_hz=(
                    self.transponder.preferred_downlink
                    + downlink_doppler_hz
                    + user_downlink_offset_hz
                    + virtual_rit_hz
                ),
                uplink_hz=None,
                user_downlink_offset_hz=user_downlink_offset_hz,
                mapped_user_uplink_offset_hz=None,
                downlink_doppler_hz=downlink_doppler_hz,
                uplink_doppler_hz=None,
            )
        plan = plan_from_offsets(
            transponder=self.transponder,
            user_downlink_offset_hz=user_downlink_offset_hz,
            user_uplink_offset_hz=user_uplink_offset_hz,
            downlink_doppler_hz=downlink_doppler_hz,
            uplink_doppler_hz=uplink_doppler_hz,
        )
        return replace(plan, downlink_hz=plan.downlink_hz + virtual_rit_hz)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _python_float(value: Any) -> float:
    return float(value)


def _session_setup_passband_hz(mode: str | None) -> int:
    normalized_mode = normalize_hamlib_mode(mode)
    if normalized_mode in {"FM", "PKTFM"}:
        return FM_SETUP_PASSBAND_HZ
    return 0


def _python_int(value: Any) -> int:
    return int(value)
