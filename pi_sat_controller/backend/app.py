from __future__ import annotations

"""Main FastAPI application for Pi-Sat.

This module owns startup and shutdown, shared runtime caches, background
refresh threads, and the browser-facing API routes. Lower-level device,
tracking, orbital, and data-ingest behavior lives in the backend submodules.
"""

from collections import deque
from contextlib import asynccontextmanager
from datetime import datetime, time, timedelta, timezone
import logging
import math
from pathlib import Path
from threading import Event, Lock, Thread
from typing import Any

from fastapi import FastAPI, HTTPException
from starlette.responses import Response
from fastapi.staticfiles import StaticFiles

from pi_sat_controller.backend.api_qso import register_qso_api
from pi_sat_controller.backend.api_satellites import register_satellites_api
from pi_sat_controller.backend.api_settings import register_settings_api
from pi_sat_controller.backend.api_system import register_system_api
from pi_sat_controller.backend.api_tracking import register_tracking_api
from pi_sat_controller.backend.config import (
    PROJECT_ROOT,
    SETTINGS_SCHEMA,
    load_config,
    load_my_satellites,
    load_settings,
    save_my_satellites,
    save_settings,
)
from pi_sat_controller.backend.controller.rx_tracking import RxTrackingManager
from pi_sat_controller.backend.device_support import (
    build_radio_client,
    build_rotator_client,
    build_rx_manager,
    load_hamlib_model_caches,
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
pass_refresh_in_progress = False
transponder_refresh_stop = Event()
transponder_refresh_thread: Thread | None = None
hamlib_radio_models_cache: list[dict[str, object]] = []
hamlib_radio_models_error: str | None = None
hamlib_rotator_models_cache: list[dict[str, object]] = []
hamlib_rotator_models_error: str | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    _reload_runtime_config()
    _refresh_transponder_profiles(reason="startup")
    try:
        _refresh_pass_cache(force_tle_download=True)
    except Exception:
        LOGGER.exception("Initial TLE/pass refresh failed; continuing startup without pass data")
    _start_pass_refresh_scheduler()
    _start_transponder_refresh_scheduler()
    try:
        yield
    finally:
        _stop_pass_refresh_scheduler()
        _stop_transponder_refresh_scheduler()
        _shutdown_runtime()


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
            "rx_write_enabled": config.rx.write_enabled,
            "tx_enabled": config.tx.enabled,
            "tx_connectivity": config.tx.connectivity,
            "tx_write_enabled": config.tx.write_enabled,
            "rotator_enabled": config.rotator.enabled,
            "rotator_connectivity": config.rotator.connectivity,
            "rotator_write_enabled": config.rotator.write_enabled,
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
    if rx_tracking_manager is not None:
        rx_tracking_manager.shutdown()
        rx_tracking_manager = None

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
        transponder=selected_satellite.transponders[transponder_index],
        deadband_hz=config.safety.frequency_deadband_hz,
        rotator_manager=rotator_manager,
        tx_radio_manager=tx_radio_manager,
        interval_s=max(0.1, config.safety.tracking_update_interval_ms / 1000.0),
    )
    return rx_tracking_manager


def _shutdown_runtime() -> None:
    global rotator_manager, rx_tracking_manager, sdr_manager, tx_radio_manager

    if rx_tracking_manager is not None:
        rx_tracking_manager.shutdown()
        rx_tracking_manager = None
    if rotator_manager is not None and hasattr(rotator_manager.client, "close"):
        rotator_manager.client.close()
    rotator_manager = None
    if tx_radio_manager is not None and hasattr(tx_radio_manager.client, "close"):
        tx_radio_manager.client.close()
    tx_radio_manager = None
    if sdr_manager is not None:
        sdr_manager.stop()
        sdr_manager = None


def _reload_runtime_config() -> None:
    global rotator_manager, sdr_manager, tx_radio_manager
    global hamlib_radio_models_cache, hamlib_radio_models_error
    global hamlib_rotator_models_cache, hamlib_rotator_models_error

    _shutdown_runtime()
    (
        hamlib_radio_models_cache,
        hamlib_radio_models_error,
        hamlib_rotator_models_cache,
        hamlib_rotator_models_error,
    ) = load_hamlib_model_caches(LOGGER)
    config = load_config()
    failure_threshold = max(1, config.safety.device_offline_failure_threshold)
    shared_local_client = None
    if uses_same_local_radio(config):
        shared_local_client = LocalHamlibClient(
            model_id=config.rx.model_id or config.tx.model_id or 0,
            serial_port=config.rx.serial_port or config.tx.serial_port,
            baud=config.rx.baud or config.tx.baud or 0,
            timeout_s=max(config.rx.timeout_s, config.tx.timeout_s),
            debug_logging=bool(config.rx.cat_debug_logging or config.tx.cat_debug_logging),
        )
    if config.rx.enabled:
        try:
            sdr_manager = build_rx_manager(
                config.rx,
                shared_local_client,
                failure_threshold=failure_threshold,
            )
            sdr_manager.start()
        except Exception as exc:
            error = f"RX startup failed: {exc}"
            LOGGER.warning(error)
            sdr_manager = FailedTrackingSdrManager(error)
    if config.tx.enabled:
        try:
            tx_radio_manager = RadioManager(
                client=build_radio_client(config.tx, "TX", shared_local_client),
                enabled=config.tx.enabled,
                write_enabled=config.tx.write_enabled,
                target_vfo=config.tx.target_vfo,
                failure_threshold=failure_threshold,
            )
        except Exception as exc:
            error = f"TX startup failed: {exc}"
            LOGGER.warning(error)
            tx_radio_manager = FailedRadioManager(error)
    if config.rotator.enabled:
        try:
            rotator_manager = RotatorManager(
                client=build_rotator_client(config.rotator),
                enabled=config.rotator.enabled,
                write_enabled=config.rotator.write_enabled,
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
                write_enabled=config.rotator.write_enabled,
            )
    try:
        _ensure_pass_cache()
    except Exception:
        LOGGER.exception("TLE/pass cache unavailable during runtime reload; continuing startup")


def _reload_rotator_config_only() -> None:
    global rotator_manager, rx_tracking_manager

    config = load_config()
    failure_threshold = max(1, config.safety.device_offline_failure_threshold)
    if rotator_manager is not None and hasattr(rotator_manager.client, "close"):
        rotator_manager.client.close()
    rotator_manager = None
    if config.rotator.enabled:
        try:
            rotator_manager = RotatorManager(
                client=build_rotator_client(config.rotator),
                enabled=config.rotator.enabled,
                write_enabled=config.rotator.write_enabled,
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
                write_enabled=config.rotator.write_enabled,
            )
    if rx_tracking_manager is not None:
        rx_tracking_manager.rotator_manager = rotator_manager


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

    global pass_cache_refreshed_at_utc, pass_refresh_in_progress
    if pass_refresh_in_progress:
        with pass_cache_lock:
            return list(pass_cache)
    pass_refresh_in_progress = True
    try:
        config = load_config()
        tle_manager = TleManager(config.tle.source_url, config.tle.cache_dir)
        LOGGER.info(
            "Pass refresh started (force_tle_download=%s)",
            force_tle_download,
        )
        if force_tle_download:
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
        satellites, min_elevation, _ = load_my_satellites()
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
        pass_refresh_in_progress = False


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


def _refresh_transponder_profiles(reason: str = "manual") -> None:
    """Refreshes stored transponder profiles for the tracked satellite list."""

    config = load_config()
    my_satellites, _, _ = load_my_satellites()
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
    get_or_create_rx_tracking_manager=_get_or_create_rx_tracking_manager,
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
    save_settings=save_settings,
    reload_runtime_config=_reload_runtime_config,
    reload_rotator_config_only=_reload_rotator_config_only,
    list_serial_devices=_list_serial_devices,
    run_device_test=lambda role, overrides: run_device_test(role, overrides, LOGGER),
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
