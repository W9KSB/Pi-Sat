from __future__ import annotations

"""Main FastAPI application for Pi-Sat.

This module owns startup and shutdown, shared runtime caches, background
refresh threads, and the browser-facing API routes. Lower-level device,
tracking, orbital, and data-ingest behavior lives in the backend submodules.
"""

from collections import deque
from collections.abc import Callable
from contextlib import asynccontextmanager
from datetime import datetime, time, timedelta, timezone
import logging
import math
from pathlib import Path
from threading import Event, Lock, RLock, Thread
from typing import Any

from fastapi import FastAPI, HTTPException
from starlette.responses import Response
from fastapi.staticfiles import StaticFiles

from pi_sat_controller.backend.api_qso import register_qso_api
from pi_sat_controller.backend.api_satellites import register_satellites_api
from pi_sat_controller.backend.api_settings import register_settings_api
from pi_sat_controller.backend.api_system import register_system_api
from pi_sat_controller.backend.api_tracking import register_tracking_api
from pi_sat_controller.backend.automation_scripts import (
    list_automation_scripts,
    run_automation_script,
)
from pi_sat_controller.backend.config import (
    PROJECT_ROOT,
    SETTINGS_SCHEMA,
    load_cat_devices,
    load_config,
    load_my_satellites,
    load_settings,
    save_my_satellites,
    save_settings,
)
from pi_sat_controller.backend.controller.rx_tracking import RxTrackingManager
from pi_sat_controller.backend.controller.autotrack import (
    AutotrackCoordinator,
    TimedLosCoordinator,
)
from pi_sat_controller.backend.device_support import (
    build_radio_client,
    build_rotator_client,
    build_rx_manager,
    load_hamlib_model_caches,
    run_cat_device_test,
    run_device_test,
    uses_same_local_radio,
)
from pi_sat_controller.backend.orbital.skyfield_engine import SkyfieldEngine
from pi_sat_controller.backend.api_serializers import (
    payload_frequency_profile_index,
    payload_norad_id,
    qth_timezone_from_config,
    serialize_frequency_profiles,
    serialize_pass,
)
from pi_sat_controller.backend.rotator.rotator_manager import (
    RotatorManager,
    disabled_rotator_snapshot,
)
from pi_sat_controller.backend.radio.radio_manager import (
    RadioManager,
    disabled_radio_snapshot,
)
from pi_sat_controller.backend.radio.local_hamlib_client import LocalHamlibClient
from pi_sat_controller.backend.radio.shared_radio_controller import (
    SharedLocalRadioController,
    SharedRadioRoleClient,
)
from pi_sat_controller.backend.runtime_fallbacks import (
    DisabledTrackingSdrManager,
    FailedRadioManager,
    FailedRotatorManager,
    FailedTrackingSdrManager,
)
from pi_sat_controller.backend.satellites.satellite_profiles import (
    load_satellite_profiles,
    upsert_satellite_transponders,
)
from pi_sat_controller.backend.satellites.tle_manager import TleManager
from pi_sat_controller.backend.satellites.transponder_source_client import (
    TransponderSourceClient,
)
from pi_sat_controller.backend.sdr.polling_sdr import (
    PollingSdrManager,
    disabled_sdr_snapshot,
)
from pi_sat_controller.backend.models import (SatellitePass, SatelliteProfile)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
LOGGER = logging.getLogger(__name__)
monitor_log_entries = deque(maxlen=100)
monitor_log_lock = Lock()


class MonitorLogHandler(logging.Handler):
    """Keeps a short in-memory log buffer for the Monitor page."""

    def emit(self, record: logging.LogRecord) -> None:
        try:
            entry = {
                "timestamp_utc": datetime.fromtimestamp(
                    record.created, tz=timezone.utc
                ).isoformat(),
                "timestamp_ms": int(record.created * 1000),
                "level": record.levelname,
                "source": record.name,
                "message": record.getMessage(),
            }
        except Exception:
            return
        with monitor_log_lock:
            monitor_log_entries.appendleft(entry)


class ConfigurableStaticFiles(StaticFiles):
    """Serves frontend assets with optional no-cache headers from config."""

    def file_response(
        self,
        full_path: str | Path,
        stat_result: Any,
        scope: dict[str, Any],
        status_code: int = 200,
    ) -> Response:
        response = super().file_response(full_path, stat_result, scope, status_code)
        try:
            caching_enabled = load_config().server.gui_resources_caching
        except Exception:
            caching_enabled = False
        if not caching_enabled:
            response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
            response.headers["Pragma"] = "no-cache"
            response.headers["Expires"] = "0"
        return response


_root_logger = logging.getLogger()
if not any(isinstance(handler, MonitorLogHandler) for handler in _root_logger.handlers):
    _root_logger.addHandler(MonitorLogHandler())


sdr_manager: PollingSdrManager | None = None
rx_tracking_manager: RxTrackingManager | None = None
rotator_manager: RotatorManager | None = None
tx_radio_manager: RadioManager | None = None
pass_cache_lock = Lock()
pass_cache: list[SatellitePass] = []
pass_cache_refreshed_at_utc: str | None = None
pass_refresh_stop = Event()
pass_refresh_thread: Thread | None = None
pass_refresh_gate = Lock()
autotrack_stop = Event()
autotrack_thread: Thread | None = None
tracking_command_lock = RLock()
transponder_refresh_stop = Event()
transponder_refresh_thread: Thread | None = None
hamlib_radio_models_cache: list[dict[str, object]] = []
hamlib_radio_models_error: str | None = None
hamlib_rotator_models_cache: list[dict[str, object]] = []
hamlib_rotator_models_error: str | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    _reload_runtime_config()
    _start_pass_refresh_scheduler()
    _start_autotrack_scheduler()
    _start_transponder_refresh_scheduler()
    Thread(
        target=_run_startup_data_refresh,
        name="startup-data-refresh",
        daemon=True,
    ).start()
    try:
        yield
    finally:
        _stop_pass_refresh_scheduler()
        _stop_autotrack_scheduler()
        _stop_transponder_refresh_scheduler()
        _shutdown_runtime()


def _run_startup_data_refresh() -> None:
    _refresh_transponder_profiles(reason="startup")
    try:
        _refresh_pass_cache(force_tle_download=True)
    except Exception:
        LOGGER.exception("Initial TLE/pass refresh failed; continuing with available cache")


app = FastAPI(title="Pi-Sat Controller", lifespan=lifespan)

def _build_status_payload() -> dict[str, object]:
    config = load_config()
    satellites = load_satellite_profiles(config.profiles.satellites_file)
    return {
        "project": "Pi-Sat Controller",
        "server": {"host": config.server.host, "port": config.server.port},
        "station": {
            "name": config.station.name,
            "latitude_deg": config.station.latitude_deg,
            "longitude_deg": config.station.longitude_deg,
            "elevation_m": config.station.elevation_m,
            "timezone": qth_timezone_from_config(),
        },
        "devices": {
            "rx_enabled": config.rx.enabled,
            "rx_connectivity": config.rx.connectivity,
            "tx_enabled": config.tx.enabled,
            "tx_connectivity": config.tx.connectivity,
            "rotator_enabled": config.rotator.enabled,
            "rotator_connectivity": config.rotator.connectivity,
        },
        "satellite_count": len(satellites),
    }


def _build_hamlib_radio_models_payload() -> dict[str, object]:
    return {
        "available": not hamlib_radio_models_error,
        "models": hamlib_radio_models_cache,
        "error": hamlib_radio_models_error,
    }


def _build_hamlib_rotator_models_payload() -> dict[str, object]:
    return {
        "available": not hamlib_rotator_models_error,
        "models": hamlib_rotator_models_cache,
        "error": hamlib_rotator_models_error,
    }


def _clear_split_for_single_role_radios(
    config,
    rx_manager,
    tx_manager,
    shared_local_radio: bool,
) -> None:
    if shared_local_radio:
        return

    if config.rx.enabled and config.rx.connectivity == "local":
        radio_manager = getattr(rx_manager, "radio_manager", None)
        if radio_manager is not None and hasattr(radio_manager, "try_set_split_mode_disabled"):
            radio_manager.try_set_split_mode_disabled(
                source="startup.rx_single_role",
                force=True,
            )

    if config.tx.enabled and config.tx.connectivity == "local":
        if tx_manager is not None and hasattr(tx_manager, "try_set_split_mode_disabled"):
            tx_manager.try_set_split_mode_disabled(
                source="startup.tx_single_role",
                force=True,
            )


def _get_or_create_rx_tracking_manager(
    norad_id: int | None = None,
    transponder_index: int = 0,
) -> RxTrackingManager:
    global rx_tracking_manager

    selected_norad = norad_id
    if selected_norad is None:
        selected_norad = 44909

    if (
        rx_tracking_manager is not None
        and rx_tracking_manager.satellite.norad_id == selected_norad
        and 0 <= transponder_index < len(rx_tracking_manager.satellite.transponders)
        and (
            rx_tracking_manager.transponder
            == rx_tracking_manager.satellite.transponders[transponder_index]
        )
    ):
        return rx_tracking_manager

    config = load_config()
    satellites = load_satellite_profiles(config.profiles.satellites_file)
    selected_satellite = next(
        (satellite for satellite in satellites if satellite.norad_id == selected_norad),
        None,
    )
    if selected_satellite is None or not selected_satellite.transponders:
        raise HTTPException(
            status_code=409,
            detail=(
                f"NORAD {selected_norad} has no local frequency profile. "
                "Pass tracking is available, but RX/TX frequency tracking needs a frequency profile."
            ),
        )
    if transponder_index < 0 or transponder_index >= len(selected_satellite.transponders):
        raise HTTPException(status_code=400, detail="Selected frequency profile is invalid")

    selected_transponder = selected_satellite.transponders[transponder_index]
    if rx_tracking_manager is not None:
        rx_tracking_manager.update_target(selected_satellite, selected_transponder)
        return rx_tracking_manager

    tle_manager = TleManager(config.tle.source_url, config.tle.cache_dir)
    tle_status = tle_manager.status()
    if not tle_status.exists:
        try:
            tle_status = tle_manager.download()
        except Exception as exc:
            raise HTTPException(
                status_code=502,
                detail=f"TLE download failed: {exc}",
            ) from exc

    try:
        orbital_engine = SkyfieldEngine(
            tle_file=tle_status.cache_file,
            latitude_deg=config.station.latitude_deg,
            longitude_deg=config.station.longitude_deg,
            elevation_m=config.station.elevation_m,
        )
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail=f"Skyfield initialization failed: {exc}",
        ) from exc

    rx_tracking_manager = RxTrackingManager(
        orbital_engine=orbital_engine,
        sdr_manager=sdr_manager or DisabledTrackingSdrManager(),
        satellite=selected_satellite,
        transponder=selected_transponder,
        deadband_hz=config.safety.frequency_deadband_hz,
        rotator_manager=rotator_manager,
        tx_radio_manager=tx_radio_manager,
        on_pass_start=None,
        on_pass_end=None,
        interval_s=max(0.1, config.safety.tracking_update_interval_ms / 1000.0),
        cat_rate_limit_hz=config.safety.cat_rate_limit_hz,
        manual_offset_readback_active_pass_only=(
            config.safety.manual_offset_readback_active_pass_only
        ),
    )
    return rx_tracking_manager


def _start_rx_tracking_manager(
    norad_id: int | None,
    transponder_index: int,
    sync_offsets: bool | None,
    source: str,
) -> RxTrackingManager:
    """Serializes target changes so concurrent browsers cannot create zombie trackers."""

    with tracking_command_lock:
        previous_norad = (
            rx_tracking_manager.satellite.norad_id
            if rx_tracking_manager is not None
            else None
        )
        manager = _get_or_create_rx_tracking_manager(norad_id, transponder_index)
        if sync_offsets is not None:
            manager.set_offset_sync(sync_offsets)
        manager.start()
        LOGGER.info(
            "Tracking target accepted source=%s previous_norad=%s norad=%s profile=%s",
            source,
            previous_norad,
            manager.satellite.norad_id,
            manager.transponder.name,
        )
        return manager


def _mutate_rx_tracking_manager(
    norad_id: int | None,
    transponder_index: int,
    action: Callable[[RxTrackingManager], Any],
) -> Any:
    """Applies a command only if the browser still references the shared target."""

    with tracking_command_lock:
        manager = rx_tracking_manager
        if manager is None:
            raise HTTPException(status_code=409, detail="Start tracking before changing it.")
        if norad_id is not None and manager.satellite.norad_id != norad_id:
            raise HTTPException(
                status_code=409,
                detail="Tracking target changed in another browser; wait for sync and try again.",
            )
        if (
            transponder_index < 0
            or transponder_index >= len(manager.satellite.transponders)
            or manager.transponder != manager.satellite.transponders[transponder_index]
        ):
            raise HTTPException(
                status_code=409,
                detail="Tracking profile changed in another browser; wait for sync and try again.",
            )
        return action(manager)


def _shutdown_runtime(preserve_tracking_manager: bool = False) -> None:
    global rotator_manager, rx_tracking_manager, sdr_manager, tx_radio_manager

    previous_rotator = rotator_manager
    previous_sdr = sdr_manager
    previous_tx = tx_radio_manager
    rotator_manager = None
    sdr_manager = None
    tx_radio_manager = None
    if not preserve_tracking_manager and rx_tracking_manager is not None:
        rx_tracking_manager.shutdown()
        rx_tracking_manager = None
    elif rx_tracking_manager is not None:
        rx_tracking_manager.update_runtime_dependencies(
            sdr_manager=DisabledTrackingSdrManager(),
            tx_radio_manager=None,
            rotator_manager=None,
        )
    if previous_sdr is not None:
        try:
            previous_sdr.stop()
        except Exception:
            LOGGER.exception("RX shutdown failed during runtime reload")
    if previous_rotator is not None:
        try:
            shutdown = getattr(previous_rotator, "shutdown", None)
            if shutdown is not None:
                shutdown()
            elif hasattr(previous_rotator.client, "close"):
                previous_rotator.client.close()
        except Exception:
            LOGGER.exception("Rotator shutdown failed during runtime reload")
    if previous_tx is not None and hasattr(previous_tx.client, "close"):
        try:
            previous_tx.client.close()
        except Exception:
            LOGGER.exception("TX shutdown failed during runtime reload")


def _reload_runtime_config() -> list[str]:
    with tracking_command_lock:
        return _reload_runtime_config_locked()


def _reload_runtime_config_locked() -> list[str]:
    global rotator_manager, sdr_manager, tx_radio_manager
    global hamlib_radio_models_cache, hamlib_radio_models_error
    global hamlib_rotator_models_cache, hamlib_rotator_models_error

    startup_errors: list[str] = []
    _shutdown_runtime(preserve_tracking_manager=True)
    (
        hamlib_radio_models_cache,
        hamlib_radio_models_error,
        hamlib_rotator_models_cache,
        hamlib_rotator_models_error,
    ) = load_hamlib_model_caches(LOGGER)
    config = load_config()
    failure_threshold = max(1, config.safety.device_offline_failure_threshold)
    shared_local_radio = uses_same_local_radio(config)
    shared_local_split_mode = (
        shared_local_radio and bool(config.tx.shared_local_split_mode)
    )
    shared_local_client: LocalHamlibClient | None = None
    shared_rx_client = None
    shared_tx_client = None
    shared_controller: SharedLocalRadioController | None = None
    shared_setup_error: str | None = None
    if shared_local_radio:
        try:
            shared_local_client = LocalHamlibClient(
                model_id=config.rx.model_id or config.tx.model_id or 0,
                serial_port=config.rx.serial_port or config.tx.serial_port,
                baud=config.rx.baud or config.tx.baud or 0,
                timeout_s=max(config.rx.timeout_s, config.tx.timeout_s),
                target_vfo=config.rx.target_vfo,
                debug_logging=bool(config.rx.cat_debug_logging or config.tx.cat_debug_logging),
                role_label="shared",
                vfo_mode=True,
            )
            shared_controller = SharedLocalRadioController(
                client=shared_local_client,
                rx_vfo=config.rx.target_vfo,
                tx_vfo=config.tx.target_vfo,
                split_enabled=shared_local_split_mode,
            )
            shared_controller.initialize()
            shared_local_split_mode = shared_controller.split_enabled
            shared_rx_client = SharedRadioRoleClient(shared_controller, "rx")
            shared_tx_client = SharedRadioRoleClient(shared_controller, "tx")
        except Exception as exc:
            shared_setup_error = str(exc)
            startup_errors.append(f"Shared radio startup failed: {exc}")
            LOGGER.warning("Shared local radio setup failed: %s", exc)
    if config.rx.enabled:
        try:
            if shared_setup_error:
                raise RuntimeError(shared_setup_error)
            sdr_manager = build_rx_manager(
                config.rx,
                shared_rx_client,
                failure_threshold=failure_threshold,
            )
            if hasattr(sdr_manager, "read_frequency_once"):
                sdr_manager.read_frequency_once()
        except Exception as exc:
            error = f"RX startup failed: {exc}"
            startup_errors.append(error)
            LOGGER.warning(error)
            sdr_manager = FailedTrackingSdrManager(error)
    if config.tx.enabled:
        try:
            if shared_setup_error:
                raise RuntimeError(shared_setup_error)
            tx_radio_manager = RadioManager(
                client=build_radio_client(config.tx, "TX", shared_tx_client),
                enabled=config.tx.enabled,
                write_enabled=True,
                target_vfo=config.tx.target_vfo,
                failure_threshold=failure_threshold,
                read_poll_enabled=not shared_local_radio,
                restore_vfo_after_write=(
                    config.rx.target_vfo if shared_local_radio else None
                ),
                split_mode_vfo=(
                    config.tx.target_vfo if shared_local_split_mode else None
                ),
                poll_target_vfo=False,
            )
            tx_radio_manager.get_frequency()
        except Exception as exc:
            error = f"TX startup failed: {exc}"
            startup_errors.append(error)
            LOGGER.warning(error)
            tx_radio_manager = FailedRadioManager(error)
    if config.rotator.enabled:
        try:
            rotator_manager = RotatorManager(
                client=build_rotator_client(config.rotator),
                enabled=config.rotator.enabled,
                write_enabled=True,
                min_elevation_deg=config.rotator.min_elevation_deg or 0.0,
                home_azimuth_deg=config.rotator.home_azimuth_deg or 0.0,
                home_elevation_deg=config.rotator.home_elevation_deg or 0.0,
                return_home_after_pass=config.rotator.return_home_after_pass,
                failure_threshold=failure_threshold,
            )
        except Exception as exc:
            error = f"Rotator startup failed: {exc}"
            startup_errors.append(error)
            LOGGER.warning(error)
            rotator_manager = FailedRotatorManager(
                error,
                enabled=config.rotator.enabled,
                write_enabled=True,
            )
    _clear_split_for_single_role_radios(
        config,
        sdr_manager,
        tx_radio_manager,
        shared_local_radio,
    )
    if rx_tracking_manager is not None:
        rx_tracking_manager.update_runtime_dependencies(
            sdr_manager=sdr_manager or DisabledTrackingSdrManager(),
            tx_radio_manager=tx_radio_manager,
            rotator_manager=rotator_manager,
            manual_offset_readback_active_pass_only=(
                config.safety.manual_offset_readback_active_pass_only
            ),
        )
    if sdr_manager is not None and not isinstance(sdr_manager, FailedTrackingSdrManager):
        sdr_manager.start()
    if rx_tracking_manager is not None:
        try:
            rx_tracking_manager.refresh_snapshot_only()
        except Exception:
            LOGGER.exception("Tracking snapshot refresh failed after runtime reload")
    try:
        tle_manager = TleManager(config.tle.source_url, config.tle.cache_dir)
        if tle_manager.status().exists:
            _ensure_pass_cache()
        else:
            LOGGER.info("Pass cache initialization deferred until startup TLE refresh")
    except Exception:
        LOGGER.exception("TLE/pass cache unavailable during runtime reload; continuing startup")
    return list(dict.fromkeys(startup_errors))


def _reload_rotator_config_only() -> None:
    with tracking_command_lock:
        _reload_rotator_config_only_locked()


def _reload_rotator_config_only_locked() -> None:
    global rotator_manager, rx_tracking_manager

    config = load_config()
    failure_threshold = max(1, config.safety.device_offline_failure_threshold)
    previous_rotator_manager = rotator_manager
    rotator_manager = None
    if rx_tracking_manager is not None:
        rx_tracking_manager.update_runtime_dependencies(
            sdr_manager=sdr_manager or DisabledTrackingSdrManager(),
            tx_radio_manager=tx_radio_manager,
            rotator_manager=None,
        )
    if previous_rotator_manager is not None:
        try:
            shutdown = getattr(previous_rotator_manager, "shutdown", None)
            if shutdown is not None:
                shutdown()
            else:
                previous_rotator_manager.stop()
        except Exception:
            LOGGER.exception("Failed to shut down rotator during control reload")
    if config.rotator.enabled:
        try:
            rotator_manager = RotatorManager(
                client=build_rotator_client(config.rotator),
                enabled=config.rotator.enabled,
                write_enabled=True,
                min_elevation_deg=config.rotator.min_elevation_deg or 0.0,
                home_azimuth_deg=config.rotator.home_azimuth_deg or 0.0,
                home_elevation_deg=config.rotator.home_elevation_deg or 0.0,
                return_home_after_pass=config.rotator.return_home_after_pass,
                failure_threshold=failure_threshold,
            )
        except Exception as exc:
            error = f"Rotator startup failed: {exc}"
            LOGGER.warning(error)
            rotator_manager = FailedRotatorManager(
                error,
                enabled=config.rotator.enabled,
                write_enabled=True,
            )
    if rx_tracking_manager is not None:
        rx_tracking_manager.update_runtime_dependencies(
            sdr_manager=sdr_manager or DisabledTrackingSdrManager(),
            tx_radio_manager=tx_radio_manager,
            rotator_manager=rotator_manager,
        )
        try:
            rx_tracking_manager.refresh_snapshot_only()
        except Exception:
            LOGGER.exception("Tracking snapshot refresh failed after rotator reload")


def _build_orbital_engine() -> SkyfieldEngine:
    config = load_config()
    tle_manager = TleManager(config.tle.source_url, config.tle.cache_dir)
    tle_status = tle_manager.status()
    if not tle_status.exists:
        try:
            tle_status = tle_manager.download()
        except Exception as exc:
            raise HTTPException(
                status_code=502,
                detail=f"TLE download failed: {exc}",
            ) from exc
    try:
        return SkyfieldEngine(
            tle_file=tle_status.cache_file,
            latitude_deg=config.station.latitude_deg,
            longitude_deg=config.station.longitude_deg,
            elevation_m=config.station.elevation_m,
        )
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail=f"Skyfield initialization failed: {exc}",
        ) from exc


def _resolve_tle_cache_file() -> Path:
    config = load_config()
    tle_manager = TleManager(config.tle.source_url, config.tle.cache_dir)
    tle_status = tle_manager.status()
    if not tle_status.exists:
        try:
            tle_status = tle_manager.download()
        except Exception as exc:
            raise HTTPException(
                status_code=502,
                detail=f"TLE download failed: {exc}",
            ) from exc
    return tle_status.cache_file


def _pass_to_dict(satellite_pass: SatellitePass) -> dict[str, object]:
    return serialize_pass(satellite_pass, qth_timezone_from_config())


def _build_qso_opportunities(
    *,
    engine: SkyfieldEngine,
    satellite_name: str,
    norad_id: int,
    grid_1: str,
    grid_1_lat: float,
    grid_1_lon: float,
    grid_1_timezone: str,
    grid_1_passes: list[SatellitePass],
    grid_2: str,
    grid_2_lat: float,
    grid_2_lon: float,
    grid_2_timezone: str,
    grid_2_passes: list[SatellitePass],
    min_overlap_seconds: int,
) -> list[dict[str, object]]:
    opportunities: list[dict[str, object]] = []
    left_index = 0
    right_index = 0

    while left_index < len(grid_1_passes) and right_index < len(grid_2_passes):
        left_pass = grid_1_passes[left_index]
        right_pass = grid_2_passes[right_index]
        overlap_start = max(left_pass.aos_utc, right_pass.aos_utc)
        overlap_end = min(left_pass.los_utc, right_pass.los_utc)

        if overlap_end > overlap_start:
            overlap_seconds = int((overlap_end - overlap_start).total_seconds())
            if overlap_seconds < min_overlap_seconds:
                if left_pass.los_utc <= right_pass.los_utc:
                    left_index += 1
                else:
                    right_index += 1
                continue
            midpoint_utc = overlap_start + (overlap_end - overlap_start) / 2
            midpoint_position = engine.get_position_at(norad_id, midpoint_utc)
            full_path_start = min(left_pass.aos_utc, right_pass.aos_utc)
            full_path_end = max(left_pass.los_utc, right_pass.los_utc)
            track_points = engine.get_ground_track(
                norad_id=norad_id,
                start_utc=full_path_start,
                end_utc=full_path_end,
                step_seconds=max(20, min(120, max(20, overlap_seconds // 24))),
            )
            footprint_points = engine.get_visibility_footprint(
                norad_id=norad_id,
                at_utc=midpoint_utc,
            )
            opportunities.append(
                {
                    "satellite_name": satellite_name,
                    "norad_id": norad_id,
                    "overlap_start_utc": overlap_start.isoformat(),
                    "overlap_end_utc": overlap_end.isoformat(),
                    "overlap_duration_seconds": overlap_seconds,
                    "track_start_utc": full_path_start.isoformat(),
                    "track_end_utc": full_path_end.isoformat(),
                    "grid_1": {
                        "locator": grid_1.upper(),
                        "latitude_deg": grid_1_lat,
                        "longitude_deg": grid_1_lon,
                        "pass": serialize_pass(left_pass, grid_1_timezone),
                    },
                    "grid_2": {
                        "locator": grid_2.upper(),
                        "latitude_deg": grid_2_lat,
                        "longitude_deg": grid_2_lon,
                        "pass": serialize_pass(right_pass, grid_2_timezone),
                    },
                    "midpoint": {
                        "utc": midpoint_utc.isoformat(),
                        "latitude_deg": round(midpoint_position.latitude_deg, 5),
                        "longitude_deg": round(midpoint_position.longitude_deg, 5),
                    },
                    "track_points": track_points,
                    "footprint_points": footprint_points,
                }
            )

        if left_pass.los_utc <= right_pass.los_utc:
            left_index += 1
        else:
            right_index += 1

    return opportunities


def _list_serial_devices() -> list[dict[str, str]]:
    target = Path("/dev/serial/by-id")
    if not target.exists():
        return []

    devices: list[dict[str, str]] = []
    for path in sorted(target.iterdir()):
        if path.name.startswith("."):
            continue
        label = path.name.replace("_", " ")
        devices.append(
            {
                "path": str(path),
                "name": path.name,
                "label": label,
            }
        )
    return devices


def _refresh_pass_cache(force_tle_download: bool) -> list[SatellitePass]:
    """Refreshes the shared pass cache used by the dashboard and satellite pages."""

    global pass_cache_refreshed_at_utc
    pass_refresh_gate.acquire()
    try:
        config = load_config()
        tle_manager = TleManager(config.tle.source_url, config.tle.cache_dir)
        LOGGER.info(
            "Pass refresh started (force_tle_download=%s)",
            force_tle_download,
        )
        tle_status = tle_manager.status()
        stale_cutoff = datetime.now(timezone.utc) - timedelta(
            hours=max(1, config.tle.stale_after_hours)
        )
        cache_is_stale = (
            not tle_status.exists
            or tle_status.downloaded_at_utc is None
            or tle_status.downloaded_at_utc < stale_cutoff
        )
        if force_tle_download or cache_is_stale:
            try:
                tle_manager.download()
            except Exception:
                LOGGER.exception("TLE download failed during pass refresh")
        try:
            engine = _build_orbital_engine()
        except HTTPException:
            LOGGER.exception("Unable to build orbital engine during pass refresh")
            with pass_cache_lock:
                return list(pass_cache)
        satellites, min_elevation, _, _ = load_my_satellites()
        all_passes: list[SatellitePass] = []
        for satellite in satellites:
            try:
                all_passes.extend(
                    engine.get_passes(
                        norad_id=satellite.norad_id,
                        satellite_name=satellite.name,
                        min_elevation_deg=min_elevation,
                        limit=128,
                        days_ahead=3,
                    )
                )
            except KeyError:
                continue

        dedup: dict[tuple[int, str], SatellitePass] = {}
        for satellite_pass in all_passes:
            key = (satellite_pass.norad_id, satellite_pass.aos_utc.isoformat())
            dedup[key] = satellite_pass

        refreshed = sorted(dedup.values(), key=lambda value: value.aos_utc)
        with pass_cache_lock:
            pass_cache.clear()
            pass_cache.extend(refreshed)
            pass_cache_refreshed_at_utc = datetime.now(timezone.utc).isoformat()
        LOGGER.info("Pass refresh complete: %s pass(es) cached", len(refreshed))
        return refreshed
    finally:
        pass_refresh_gate.release()


def _ensure_pass_cache() -> None:
    with pass_cache_lock:
        has_cache = bool(pass_cache)
    if not has_cache:
        _refresh_pass_cache(force_tle_download=False)


def _seconds_until_next_midnight(timezone_name: str) -> float:
    from zoneinfo import ZoneInfo

    now_local = datetime.now(ZoneInfo(timezone_name))
    next_midnight = datetime.combine(
        now_local.date() + timedelta(days=1),
        time.min,
        tzinfo=now_local.tzinfo,
    )
    seconds = (next_midnight - now_local).total_seconds()
    return max(seconds, 1.0)


def _run_pass_refresh_scheduler() -> None:
    """Runs the recurring six-hour TLE and pass refresh loop."""

    while not pass_refresh_stop.is_set():
        wait_seconds = 6 * 60 * 60
        if pass_refresh_stop.wait(wait_seconds):
            break
        try:
            _refresh_pass_cache(force_tle_download=True)
        except Exception:
            LOGGER.exception("Scheduled TLE/pass refresh failed")


def _start_pass_refresh_scheduler() -> None:
    global pass_refresh_thread
    if pass_refresh_thread is not None:
        return
    _ensure_pass_cache()
    pass_refresh_stop.clear()
    pass_refresh_thread = Thread(
        target=_run_pass_refresh_scheduler,
        name="pass-refresh-scheduler",
        daemon=True,
    )
    pass_refresh_thread.start()


def _stop_pass_refresh_scheduler() -> None:
    global pass_refresh_thread
    pass_refresh_stop.set()
    if pass_refresh_thread is not None:
        pass_refresh_thread.join(timeout=2.0)
        pass_refresh_thread = None


def _load_autotrack_options() -> tuple[set[int], bool]:
    _, _, enabled, autotrack_norads = load_my_satellites()
    return autotrack_norads, enabled


def _get_cached_passes() -> list[SatellitePass]:
    with pass_cache_lock:
        return list(pass_cache)


def _start_autotrack_pass(satellite_pass: SatellitePass) -> bool:
    with tracking_command_lock:
        _, _, enabled, _ = load_my_satellites()
        if not enabled:
            return False
        current_sync = (
            bool(rx_tracking_manager.snapshot().sync_offsets)
            if rx_tracking_manager is not None
            else True
        )
        _start_rx_tracking_manager(
            satellite_pass.norad_id,
            0,
            current_sync,
            "backend_autotrack",
        )
        return True


def _run_pre_aos_automation(satellite_pass: SatellitePass) -> None:
    _trigger_automation_script_event(
        "aos",
        _automation_context_for_pass(satellite_pass, "AOS"),
    )


def _run_timed_los_automation(satellite_pass: SatellitePass) -> None:
    _trigger_automation_script_event(
        "los",
        _automation_context_for_pass(satellite_pass, "LOS"),
    )


def _automation_context_for_pass(
    satellite_pass: SatellitePass,
    event_name: str,
) -> dict[str, object]:
    manager = rx_tracking_manager
    snapshot = manager.snapshot() if manager is not None else None
    if manager is not None and manager.satellite.norad_id != satellite_pass.norad_id:
        snapshot = None
    return {
        "event": event_name,
        "norad_id": satellite_pass.norad_id,
        "satellite_name": satellite_pass.satellite_name,
        "aos_utc": satellite_pass.aos_utc.isoformat(),
        "los_utc": satellite_pass.los_utc.isoformat(),
        "transponder_name": getattr(snapshot, "transponder_name", None),
        "azimuth_deg": getattr(snapshot, "azimuth_deg", None),
        "elevation_deg": getattr(snapshot, "elevation_deg", None),
        "latitude_deg": getattr(snapshot, "latitude_deg", None),
        "longitude_deg": getattr(snapshot, "longitude_deg", None),
        "range_km": getattr(snapshot, "range_km", None),
        "range_rate_m_s": getattr(snapshot, "range_rate_m_s", None),
        "target_rx_hz": getattr(snapshot, "target_rx_hz", None),
        "target_tx_hz": getattr(snapshot, "calculated_tx_hz", None),
    }


def _get_active_tracking_norad() -> int | None:
    manager = rx_tracking_manager
    if manager is None or not manager.snapshot().active:
        return None
    return manager.satellite.norad_id


autotrack_coordinator = AutotrackCoordinator(
    load_options=_load_autotrack_options,
    get_passes=_get_cached_passes,
    start_pass=_start_autotrack_pass,
    run_pre_aos=_run_pre_aos_automation,
    logger=LOGGER,
)

timed_los_coordinator = TimedLosCoordinator(
    get_active_norad=_get_active_tracking_norad,
    get_passes=_get_cached_passes,
    run_los=_run_timed_los_automation,
    logger=LOGGER,
)


def _run_autotrack_scheduler() -> None:
    """Runs timed LOS before the authoritative upcoming-pass selection loop."""

    while not autotrack_stop.is_set():
        try:
            timed_los_coordinator.tick()
        except Exception:
            LOGGER.exception("Timed LOS evaluation failed")
        try:
            autotrack_coordinator.tick()
        except Exception:
            LOGGER.exception("Autotrack evaluation failed")
        if autotrack_stop.wait(1.0):
            break


def _start_autotrack_scheduler() -> None:
    global autotrack_thread
    if autotrack_thread is not None:
        return
    autotrack_stop.clear()
    autotrack_thread = Thread(
        target=_run_autotrack_scheduler,
        name="autotrack-scheduler",
        daemon=True,
    )
    autotrack_thread.start()


def _stop_autotrack_scheduler() -> None:
    global autotrack_thread
    autotrack_stop.set()
    if autotrack_thread is not None:
        autotrack_thread.join(timeout=2.0)
        autotrack_thread = None


def _handle_autotrack_changed(enabled: bool) -> None:
    autotrack_coordinator.reset()
    if not enabled:
        return
    try:
        autotrack_coordinator.tick()
    except Exception:
        LOGGER.exception("Autotrack evaluation failed after enable")


def _refresh_transponder_profiles(reason: str = "manual") -> None:
    """Refreshes stored transponder profiles for the tracked satellite list."""

    config = load_config()
    my_satellites, _, _, _ = load_my_satellites()
    existing_profiles = {
        satellite.norad_id: satellite
        for satellite in load_satellite_profiles(config.profiles.satellites_file)
    }
    client = TransponderSourceClient()
    LOGGER.info(
        "Transponder refresh started (%s) for %s tracked satellite(s)",
        reason,
        len(my_satellites),
    )
    updated_count = 0
    for satellite in my_satellites:
        try:
            transponders = client.get_transponders(satellite.norad_id)
        except Exception:
            LOGGER.exception(
                "Transponder refresh failed for %s (%s)",
                satellite.name,
                satellite.norad_id,
            )
            continue
        if not transponders:
            LOGGER.warning(
                "No transponders returned for %s (%s)",
                satellite.name,
                satellite.norad_id,
            )
            continue
        upsert_satellite_transponders(
            config.profiles.satellites_file,
            SatelliteProfile(
                name=existing_profiles.get(satellite.norad_id, satellite).name,
                norad_id=satellite.norad_id,
                favorite=True,
                transponders=transponders,
            ),
        )
        updated_count += 1
        LOGGER.info(
            "Transponder refresh updated %s (%s) with %s profile(s)",
            satellite.name,
            satellite.norad_id,
            len(transponders),
        )
    LOGGER.info(
        "Transponder refresh complete (%s): %s/%s satellites updated",
        reason,
        updated_count,
        len(my_satellites),
    )


def _run_transponder_refresh_scheduler() -> None:
    config = load_config()
    timezone_name = qth_timezone_name(
        config.station.latitude_deg,
        config.station.longitude_deg,
    )
    while not transponder_refresh_stop.is_set():
        wait_seconds = _seconds_until_next_midnight(timezone_name)
        if transponder_refresh_stop.wait(wait_seconds):
            break
        _refresh_transponder_profiles(reason="nightly")


def _start_transponder_refresh_scheduler() -> None:
    global transponder_refresh_thread
    if transponder_refresh_thread is not None:
        return
    transponder_refresh_stop.clear()
    transponder_refresh_thread = Thread(
        target=_run_transponder_refresh_scheduler,
        name="transponder-refresh-scheduler",
        daemon=True,
    )
    transponder_refresh_thread.start()


def _stop_transponder_refresh_scheduler() -> None:
    global transponder_refresh_thread
    transponder_refresh_stop.set()
    if transponder_refresh_thread is not None:
        transponder_refresh_thread.join(timeout=2.0)
        transponder_refresh_thread = None


def _trigger_automation_script_event(
    event_name: str,
    context: dict[str, object],
) -> None:
    Thread(
        target=_run_automation_script_event,
        args=(event_name, context),
        name=f"automation-script-{event_name}",
        daemon=True,
    ).start()


def _run_automation_script_event(
    event_name: str,
    context: dict[str, object],
) -> None:
    try:
        config = load_config()
        script_name = (
            config.automation.aos_script
            if event_name == "aos"
            else config.automation.los_script
        ).strip()
        if not script_name:
            return
        event_label = event_name.upper()
        LOGGER.info(
            "automation_script_event starting event=%s script=%s satellite=%s norad=%s",
            event_label,
            script_name,
            context.get("satellite_name"),
            context.get("norad_id"),
        )
        result = run_automation_script(
            script_name,
            event_label,
            context=context,
        )
        LOGGER.info(
            "automation_script_event finished event=%s script=%s ok=%s exit_code=%s duration_ms=%s",
            event_label,
            result["script_name"],
            result["ok"],
            result["exit_code"],
            result["duration_ms"],
        )
        if result.get("stdout"):
            LOGGER.info(
                "automation_script_event stdout script=%s output=%s",
                result["script_name"],
                result["stdout"],
            )
        if result.get("stderr"):
            LOGGER.warning(
                "automation_script_event stderr script=%s output=%s",
                result["script_name"],
                result["stderr"],
            )
    except Exception:
        LOGGER.exception(
            "automation_script_event failed event=%s satellite=%s norad=%s",
            event_name.upper(),
            context.get("satellite_name"),
            context.get("norad_id"),
        )


def _run_automation_script_test(event_name: str, script_name: str) -> dict[str, object]:
    event_label = f"TEST_{event_name.upper()}"
    LOGGER.info(
        "automation_script_test starting event=%s script=%s",
        event_label,
        script_name or "none",
    )
    result = run_automation_script(script_name, event_label)
    LOGGER.info(
        "automation_script_test finished event=%s script=%s ok=%s exit_code=%s duration_ms=%s",
        event_label,
        result["script_name"],
        result["ok"],
        result["exit_code"],
        result["duration_ms"],
    )
    if result.get("stdout"):
        LOGGER.info(
            "automation_script_test stdout script=%s output=%s",
            result["script_name"],
            result["stdout"],
        )
    if result.get("stderr"):
        LOGGER.warning(
            "automation_script_test stderr script=%s output=%s",
            result["script_name"],
            result["stderr"],
        )
    return result


register_system_api(
    app,
    get_monitor_entries=lambda: monitor_log_entries,
    monitor_log_lock=monitor_log_lock,
    build_status=_build_status_payload,
    get_hamlib_radio_models_payload=_build_hamlib_radio_models_payload,
    get_hamlib_rotator_models_payload=_build_hamlib_rotator_models_payload,
)

register_tracking_api(
    app,
    get_sdr_manager=lambda: sdr_manager,
    get_tx_radio_manager=lambda: tx_radio_manager,
    get_rx_tracking_manager=lambda: rx_tracking_manager,
    get_rotator_manager=lambda: rotator_manager,
    start_rx_tracking_manager=_start_rx_tracking_manager,
    mutate_rx_tracking_manager=_mutate_rx_tracking_manager,
    get_autotrack_enabled=lambda: load_my_satellites()[2],
    disabled_sdr_snapshot=disabled_sdr_snapshot,
    disabled_radio_snapshot=disabled_radio_snapshot,
    disabled_rotator_snapshot=disabled_rotator_snapshot,
    payload_norad_id=payload_norad_id,
    payload_frequency_profile_index=payload_frequency_profile_index,
    build_orbital_engine=_build_orbital_engine,
)

register_settings_api(
    app,
    logger=LOGGER,
    settings_schema=SETTINGS_SCHEMA,
    load_settings=load_settings,
    load_cat_devices=load_cat_devices,
    save_settings=save_settings,
    reload_runtime_config=_reload_runtime_config,
    reload_rotator_config_only=_reload_rotator_config_only,
    list_serial_devices=_list_serial_devices,
    run_device_test=lambda role, overrides, cat_devices=None: run_device_test(
        role,
        overrides,
        LOGGER,
        cat_devices,
    ),
    run_cat_device_test=lambda device: run_cat_device_test(device, LOGGER),
    list_automation_scripts=lambda: [script.to_dict() for script in list_automation_scripts()],
    run_automation_script_test=lambda event_name, script_name: _run_automation_script_test(
        event_name,
        script_name,
    ),
    build_status=_build_status_payload,
)

register_satellites_api(
    app,
    logger=LOGGER,
    serialize_frequency_profiles=serialize_frequency_profiles,
    qth_timezone_from_config=qth_timezone_from_config,
    build_orbital_engine=_build_orbital_engine,
    load_my_satellites=load_my_satellites,
    save_my_satellites=save_my_satellites,
    ensure_pass_cache=_ensure_pass_cache,
    refresh_pass_cache=_refresh_pass_cache,
    get_pass_cache=lambda: pass_cache,
    pass_cache_lock=pass_cache_lock,
    pass_to_dict=_pass_to_dict,
    get_pass_cache_refreshed_at_utc=lambda: pass_cache_refreshed_at_utc,
    on_autotrack_changed=_handle_autotrack_changed,
)

register_qso_api(
    app,
    resolve_tle_cache_file=_resolve_tle_cache_file,
    load_my_satellites=load_my_satellites,
    build_qso_opportunities=_build_qso_opportunities,
)


frontend_dir = PROJECT_ROOT / "frontend"
if frontend_dir.exists():
    app.mount("/", ConfigurableStaticFiles(directory=frontend_dir, html=True), name="frontend")
