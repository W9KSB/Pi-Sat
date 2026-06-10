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
from pathlib import Path
from threading import Event, Lock, Thread
from typing import Any

from fastapi import Body, FastAPI, HTTPException, Query
from starlette.responses import Response
from fastapi.staticfiles import StaticFiles

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
from pi_sat_controller.backend.orbital.skyfield_engine import SkyfieldEngine
from pi_sat_controller.backend.rotator.rotator_manager import (
    RotatorManager,
    disabled_rotator_snapshot,
)
from pi_sat_controller.backend.rotator.local_rotctld_client import LocalRotctldClient
from pi_sat_controller.backend.rotator.hamlib_rotator_models import load_hamlib_rotator_models
from pi_sat_controller.backend.rotator.rotctld_client import RotctldClient
from pi_sat_controller.backend.radio.hamlib_client import HamlibClient
from pi_sat_controller.backend.radio.local_hamlib_client import LocalHamlibClient
from pi_sat_controller.backend.radio.hamlib_models import load_hamlib_radio_models
from pi_sat_controller.backend.radio.radio_manager import (
    RadioManager,
    disabled_radio_snapshot,
)
from pi_sat_controller.backend.satellites.satellite_profiles import (
    load_satellite_profiles,
    upsert_satellite_transponders,
)
from pi_sat_controller.backend.satellites.tle_manager import TleManager
from pi_sat_controller.backend.satellites.transponder_source_client import (
    TransponderSourceClient,
)
from pi_sat_controller.backend.timezone_utils import (
    qth_timezone_name,
    to_local_iso,
    to_local_label,
)
from pi_sat_controller.backend.sdr.polling_sdr import (
    PollingSdrManager,
    PollingRadioFrequencyManager,
    disabled_sdr_snapshot,
)
from pi_sat_controller.backend.models import MySatellite, SatellitePass, SatelliteProfile

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


class DisabledTrackingSdrManager:
    """Fallback object used when SDR polling is disabled in config."""

    def snapshot(self):
        return disabled_sdr_snapshot()

    def set_frequency(self, frequency_hz: int):
        return disabled_sdr_snapshot()


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


@app.get("/api/monitor/logs")
def get_monitor_logs() -> dict[str, object]:
    with monitor_log_lock:
        entries = list(monitor_log_entries)
    return {"entries": entries}


@app.get("/api/status")
def get_status() -> dict[str, object]:
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
            "timezone": qth_timezone_name(
                config.station.latitude_deg,
                config.station.longitude_deg,
            ),
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


@app.get("/api/hamlib/radio-models")
def get_hamlib_radio_models() -> dict[str, object]:
    return {
        "available": not hamlib_radio_models_error,
        "models": hamlib_radio_models_cache,
        "error": hamlib_radio_models_error,
    }


@app.get("/api/hamlib/rotator-models")
def get_hamlib_rotator_models() -> dict[str, object]:
    return {
        "available": not hamlib_rotator_models_error,
        "models": hamlib_rotator_models_cache,
        "error": hamlib_rotator_models_error,
    }


@app.get("/api/satellites")
def get_satellites() -> list[dict[str, object]]:
    config = load_config()
    profile_by_norad = {
        satellite.norad_id: satellite
        for satellite in load_satellite_profiles(config.profiles.satellites_file)
    }
    my_satellites, _, _ = load_my_satellites()
    return [
        {
            "name": my_satellite.name,
            "norad_id": my_satellite.norad_id,
            "favorite": True,
            "frequency_profiles": [
                {
                    "name": transponder.name,
                    "type": transponder.type,
                    "uplink_low": transponder.uplink_low,
                    "uplink_high": transponder.uplink_high,
                    "downlink_low": transponder.downlink_low,
                    "downlink_high": transponder.downlink_high,
                    "uplink_mode": transponder.uplink_mode,
                    "downlink_mode": transponder.downlink_mode,
                    "inverted": transponder.inverted,
                    "ratio": transponder.ratio,
                    "preferred_uplink": transponder.preferred_uplink,
                    "preferred_downlink": transponder.preferred_downlink,
                    "tone": transponder.tone,
                }
                for transponder in profile_by_norad.get(
                    my_satellite.norad_id,
                    SatelliteProfile(
                        name=my_satellite.name,
                        norad_id=my_satellite.norad_id,
                        favorite=True,
                        transponders=[],
                    ),
                ).transponders
            ],
        }
        for my_satellite in my_satellites
    ]


@app.get("/api/devices/sdr/frequency")
def get_sdr_frequency() -> dict[str, object]:
    if sdr_manager is None:
        return disabled_sdr_snapshot().to_dict()

    return sdr_manager.snapshot().to_dict()


@app.post("/api/devices/sdr/frequency")
def set_sdr_frequency(payload: dict[str, Any] = Body(...)) -> dict[str, object]:
    if sdr_manager is None:
        raise HTTPException(
            status_code=409,
            detail="RX control is off or not configured",
        )

    try:
        frequency_hz = int(payload["frequency_hz"])
    except (KeyError, TypeError, ValueError) as exc:
        raise HTTPException(
            status_code=400,
            detail="Request body must include integer frequency_hz",
        ) from exc

    try:
        return sdr_manager.set_frequency(frequency_hz).to_dict()
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.get("/api/devices/tx/frequency")
def get_tx_frequency() -> dict[str, object]:
    if tx_radio_manager is None:
        return disabled_radio_snapshot().to_dict()
    return tx_radio_manager.poll_once().to_dict()


@app.get("/api/tracking/rx")
def get_rx_tracking() -> dict[str, object]:
    if rx_tracking_manager is None:
        return {
            "active": False,
            "sync_offsets": True,
            "error": "RX tracking has not been started",
        }
    return rx_tracking_manager.snapshot().to_dict()


@app.post("/api/tracking/rx/start")
def start_rx_tracking(payload: dict[str, Any] | None = Body(default=None)) -> dict[str, object]:
    norad_id = None
    frequency_profile_index = 0
    if payload:
        raw_norad = payload.get("norad_id")
        norad_id = int(raw_norad) if raw_norad else None
        raw_profile_index = payload.get("frequency_profile_index")
        frequency_profile_index = int(raw_profile_index) if raw_profile_index else 0
    manager = _get_or_create_rx_tracking_manager(norad_id, frequency_profile_index)
    if payload and "sync_offsets" in payload:
        manager.set_offset_sync(bool(payload["sync_offsets"]))
    manager.start()
    return manager.snapshot().to_dict()


@app.post("/api/tracking/rx/stop")
def stop_rx_tracking() -> dict[str, object]:
    if rx_tracking_manager is None:
        return {"active": False, "error": None}
    rx_tracking_manager.stop()
    return rx_tracking_manager.snapshot().to_dict()


@app.post("/api/tracking/rx/reset-offset")
def reset_rx_tracking_offset(payload: dict[str, Any] | None = Body(default=None)) -> dict[str, object]:
    payload = payload or {}
    manager = _get_or_create_rx_tracking_manager(
        _payload_norad_id(payload),
        _payload_frequency_profile_index(payload),
    )
    if "sync_offsets" in payload:
        manager.set_offset_sync(bool(payload["sync_offsets"]))
    return manager.reset_offset().to_dict()


@app.post("/api/tracking/offset-sync")
def set_tracking_offset_sync(payload: dict[str, Any] = Body(...)) -> dict[str, object]:
    manager = _get_or_create_rx_tracking_manager(
        _payload_norad_id(payload),
        _payload_frequency_profile_index(payload),
    )
    return manager.set_offset_sync(bool(payload.get("enabled", True))).to_dict()


@app.post("/api/tracking/rx/step")
def step_rx_tracking_offset(payload: dict[str, Any] = Body(...)) -> dict[str, object]:
    manager = _get_or_create_rx_tracking_manager(
        _payload_norad_id(payload),
        _payload_frequency_profile_index(payload),
    )
    if "sync_offsets" in payload:
        manager.set_offset_sync(bool(payload["sync_offsets"]))
    try:
        step_hz = int(payload["step_hz"])
    except (KeyError, TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail="Request body must include integer step_hz") from exc
    return manager.adjust_downlink_offset(step_hz).to_dict()


@app.post("/api/tracking/tx/step")
def step_tx_tracking_offset(payload: dict[str, Any] = Body(...)) -> dict[str, object]:
    manager = _get_or_create_rx_tracking_manager(
        _payload_norad_id(payload),
        _payload_frequency_profile_index(payload),
    )
    if "sync_offsets" in payload:
        manager.set_offset_sync(bool(payload["sync_offsets"]))
    try:
        step_hz = int(payload["step_hz"])
    except (KeyError, TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail="Request body must include integer step_hz") from exc
    return manager.adjust_uplink_offset(step_hz).to_dict()


@app.get("/api/devices/rotator")
def get_rotator_status() -> dict[str, object]:
    if rotator_manager is None:
        return disabled_rotator_snapshot().to_dict()
    return rotator_manager.poll_once().to_dict()


@app.post("/api/devices/rotator/stop")
def stop_rotator() -> dict[str, object]:
    if rotator_manager is None:
        raise HTTPException(
            status_code=409,
            detail="Rotator control is off or not configured",
        )
    return rotator_manager.stop().to_dict()


@app.post("/api/devices/rotator/move")
def move_rotator(payload: dict[str, Any] = Body(...)) -> dict[str, object]:
    if rotator_manager is None:
        raise HTTPException(
            status_code=409,
            detail="Rotator control is off or not configured",
        )
    try:
        azimuth_deg = float(payload["azimuth_deg"])
        elevation_deg = float(payload["elevation_deg"])
    except (KeyError, TypeError, ValueError) as exc:
        raise HTTPException(
            status_code=400,
            detail="Request body must include numeric azimuth_deg and elevation_deg",
        ) from exc
    if not (0.0 <= azimuth_deg <= 359.0):
        raise HTTPException(status_code=400, detail="Azimuth must be between 0 and 359")
    if not (0.0 <= elevation_deg <= 90.0):
        raise HTTPException(status_code=400, detail="Elevation must be between 0 and 90")
    snapshot = rotator_manager.manual_position(azimuth_deg, elevation_deg)
    if snapshot.pass_active:
        raise HTTPException(status_code=409, detail="PASS ACTIVE, manual mode disabled")
    return snapshot.to_dict()


@app.post("/api/devices/rotator/home")
def home_rotator() -> dict[str, object]:
    if rotator_manager is None:
        raise HTTPException(
            status_code=409,
            detail="Rotator control is off or not configured",
        )
    snapshot = rotator_manager.send_home()
    if snapshot.pass_active:
        raise HTTPException(status_code=409, detail="PASS ACTIVE, manual mode disabled")
    return snapshot.to_dict()


@app.get("/api/settings")
def get_settings() -> dict[str, object]:
    return {
        "schema": SETTINGS_SCHEMA,
        "settings": load_settings(),
    }


@app.post("/api/settings")
def update_settings(payload: dict[str, Any] = Body(...)) -> dict[str, object]:
    try:
        save_settings(payload.get("settings", {}))
        _reload_runtime_config()
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {
        "schema": SETTINGS_SCHEMA,
        "settings": load_settings(),
    }


@app.post("/api/runtime/reload")
def reload_runtime() -> dict[str, object]:
    try:
        _reload_runtime_config()
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return get_status()


@app.get("/api/serial-devices")
def get_serial_devices() -> dict[str, object]:
    return {"devices": _list_serial_devices()}


@app.post("/api/device-controls")
def update_device_controls(payload: dict[str, Any] = Body(...)) -> dict[str, object]:
    settings = load_settings()
    rx_changed = False
    tx_changed = False
    rotator_changed = False
    if "rx_enabled" in payload:
        next_value = "true" if bool(payload["rx_enabled"]) else "false"
        rx_changed = settings["rx"]["enabled"] != next_value
        settings["rx"]["enabled"] = next_value
    if "tx_enabled" in payload:
        next_value = "true" if bool(payload["tx_enabled"]) else "false"
        tx_changed = settings["tx"]["enabled"] != next_value
        settings["tx"]["enabled"] = next_value
    if "rotator_enabled" in payload:
        next_value = "true" if bool(payload["rotator_enabled"]) else "false"
        rotator_changed = settings["rotator"]["enabled"] != next_value
        settings["rotator"]["enabled"] = next_value
    try:
        save_settings(settings)
        if rx_changed or tx_changed:
            _reload_runtime_config()
        elif rotator_changed:
            _reload_rotator_config_only()
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return get_status()


@app.get("/api/frequency-profiles/{norad_id}")
def get_frequency_profiles(norad_id: int) -> dict[str, object]:
    config = load_config()
    profile = next(
        (
            satellite
            for satellite in load_satellite_profiles(config.profiles.satellites_file)
            if satellite.norad_id == norad_id
        ),
        None,
    )
    return {
        "norad_id": norad_id,
        "frequency_profiles": [
            {
                "name": transponder.name,
                "type": transponder.type,
                "uplink_low": transponder.uplink_low,
                "uplink_high": transponder.uplink_high,
                "downlink_low": transponder.downlink_low,
                "downlink_high": transponder.downlink_high,
                "uplink_mode": transponder.uplink_mode,
                "downlink_mode": transponder.downlink_mode,
                "inverted": transponder.inverted,
                "ratio": transponder.ratio,
                "preferred_uplink": transponder.preferred_uplink,
                "preferred_downlink": transponder.preferred_downlink,
                "tone": transponder.tone,
            }
            for transponder in (profile.transponders if profile else [])
        ],
    }


@app.post("/api/frequency-profiles/{norad_id}/update")
def update_frequency_profiles(norad_id: int) -> dict[str, object]:
    config = load_config()
    my_satellites, _, _ = load_my_satellites()
    satellite_name = next(
        (
            satellite.name
            for satellite in my_satellites
            if satellite.norad_id == norad_id
        ),
        str(norad_id),
    )
    LOGGER.info(
        "Manual transponder refresh requested for %s (%s)",
        satellite_name,
        norad_id,
    )

    try:
        transponders = TransponderSourceClient().get_transponders(norad_id)
    except Exception as exc:
        LOGGER.exception(
            "Manual transponder refresh failed for %s (%s)",
            satellite_name,
            norad_id,
        )
        raise HTTPException(
            status_code=502,
            detail=f"Frequency profile update failed: {exc}",
        ) from exc

    if not transponders:
        LOGGER.warning(
            "Manual transponder refresh returned no profiles for %s (%s)",
            satellite_name,
            norad_id,
        )
        raise HTTPException(
            status_code=404,
            detail=f"No frequency profiles found for NORAD {norad_id}",
        )

    profile = upsert_satellite_transponders(
        config.profiles.satellites_file,
        SatelliteProfile(
            name=satellite_name,
            norad_id=norad_id,
            favorite=True,
            transponders=transponders,
        ),
    )
    LOGGER.info(
        "Manual transponder refresh updated %s (%s) with %s profile(s)",
        profile.name,
        profile.norad_id,
        len(profile.transponders),
    )
    return {
        "norad_id": profile.norad_id,
        "name": profile.name,
        "imported": len(profile.transponders),
        "frequency_profiles": [
            {"name": transponder.name}
            for transponder in profile.transponders
        ],
    }


@app.get("/api/my-satellites")
def get_my_satellites() -> dict[str, object]:
    satellites, min_elevation, autotrack = load_my_satellites()
    return {
        "satellites": [
            {"norad_id": satellite.norad_id, "name": satellite.name}
            for satellite in satellites
        ],
        "min_pass_elevation_deg": min_elevation,
        "autotrack_next_pass": autotrack,
    }


@app.post("/api/my-satellites")
def add_my_satellite(payload: dict[str, Any] = Body(...)) -> dict[str, object]:
    try:
        norad_id = int(payload["norad_id"])
    except (KeyError, TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail="NORAD ID is required") from exc

    engine = _build_orbital_engine()
    if norad_id not in engine.satellites:
        raise HTTPException(
            status_code=404,
            detail=f"NORAD {norad_id} was not found in the current TLE cache",
        )

    satellites, min_elevation, autotrack = load_my_satellites()
    name = str(payload.get("name") or engine.satellites[norad_id].name or norad_id)
    updated = [satellite for satellite in satellites if satellite.norad_id != norad_id]
    updated.append(MySatellite(norad_id=norad_id, name=name))
    save_my_satellites(updated, min_elevation, autotrack)
    return get_my_satellites()


@app.delete("/api/my-satellites/{norad_id}")
def delete_my_satellite(norad_id: int) -> dict[str, object]:
    satellites, min_elevation, autotrack = load_my_satellites()
    save_my_satellites(
        [satellite for satellite in satellites if satellite.norad_id != norad_id],
        min_elevation,
        autotrack,
    )
    return get_my_satellites()


@app.post("/api/my-satellites/options")
def update_my_satellite_options(payload: dict[str, Any] = Body(...)) -> dict[str, object]:
    satellites, min_elevation, autotrack = load_my_satellites()
    if "min_pass_elevation_deg" in payload:
        min_elevation = float(payload["min_pass_elevation_deg"])
    if "autotrack_next_pass" in payload:
        autotrack = bool(payload["autotrack_next_pass"])
    save_my_satellites(satellites, min_elevation, autotrack)
    return get_my_satellites()


@app.get("/api/my-satellites/passes")
def get_my_satellite_passes(hours: int = Query(default=48, ge=1, le=72)) -> dict[str, object]:
    try:
        _ensure_pass_cache()
    except Exception:
        LOGGER.exception("Pass cache refresh failed while serving /api/my-satellites/passes")
        return {"hours": hours, "passes": []}

    now_utc = datetime.now(timezone.utc)
    horizon_utc = now_utc + timedelta(hours=hours)
    satellites, _, _ = load_my_satellites()
    selected_norad_ids = {satellite.norad_id for satellite in satellites}

    grouped: dict[int, list[dict[str, object]]] = {norad_id: [] for norad_id in selected_norad_ids}
    with pass_cache_lock:
        cached = list(pass_cache)

    for satellite_pass in cached:
        if satellite_pass.norad_id not in selected_norad_ids:
            continue
        if satellite_pass.los_utc <= now_utc:
            continue
        if satellite_pass.aos_utc > horizon_utc:
            continue
        grouped.setdefault(satellite_pass.norad_id, []).append(_pass_to_dict(satellite_pass))

    return {
        "hours": hours,
        "passes": [
            {"norad_id": norad_id, "passes": grouped.get(norad_id, [])}
            for norad_id in sorted(grouped)
        ],
    }


@app.post("/api/passes/refresh")
def refresh_passes() -> dict[str, object]:
    refreshed = _refresh_pass_cache(force_tle_download=True)
    return {
        "ok": True,
        "refreshed_at_utc": pass_cache_refreshed_at_utc,
        "pass_count": len(refreshed),
    }


@app.post("/api/tle/refresh")
def refresh_tle_data() -> dict[str, object]:
    config = load_config()
    tle_manager = TleManager(config.tle.source_url, config.tle.cache_dir)
    LOGGER.info("Manual TLE refresh requested")
    try:
        status = tle_manager.download()
    except Exception as exc:
        LOGGER.exception("Manual TLE refresh failed")
        raise HTTPException(status_code=502, detail=f"TLE refresh failed: {exc}") from exc
    _refresh_pass_cache(force_tle_download=False)
    LOGGER.info("Manual TLE refresh completed at %s", status.downloaded_at_utc)
    return {
        "ok": True,
        "refreshed_at_utc": status.downloaded_at_utc.isoformat() if status.downloaded_at_utc else None,
    }


@app.get("/api/passes/next")
def get_next_passes(
    norad_ids: str | None = Query(default=None),
) -> list[dict[str, object]]:
    try:
        _ensure_pass_cache()
    except Exception:
        LOGGER.exception("Pass cache refresh failed while serving /api/passes/next")
        return []
    now_utc = datetime.now(timezone.utc)
    satellites, _, _ = load_my_satellites()
    if norad_ids:
        selected_norads = {
            int(value.strip())
            for value in norad_ids.split(",")
            if value.strip().isdigit()
        }
        satellites = [
            satellite
            for satellite in satellites
            if satellite.norad_id in selected_norads
        ]
    selected_norad_ids = {satellite.norad_id for satellite in satellites}
    with pass_cache_lock:
        cached = list(pass_cache)
    passes = [
        satellite_pass
        for satellite_pass in cached
        if satellite_pass.norad_id in selected_norad_ids
        and satellite_pass.los_utc > now_utc
    ][:15]
    return [_pass_to_dict(satellite_pass) for satellite_pass in passes]


@app.get("/api/tracked-satellites/positions")
def get_tracked_satellite_positions(
    norad_ids: str | None = Query(default=None),
) -> dict[str, object]:
    try:
        engine = _build_orbital_engine()
    except HTTPException:
        config = load_config()
        timezone_name = qth_timezone_name(
            config.station.latitude_deg,
            config.station.longitude_deg,
        )
        return {
            "timezone": timezone_name,
            "positions": [],
        }
    satellites, _, _ = load_my_satellites()
    if norad_ids:
        selected_norads = {
            int(value.strip())
            for value in norad_ids.split(",")
            if value.strip().isdigit()
        }
        satellites = [
            satellite
            for satellite in satellites
            if satellite.norad_id in selected_norads
        ]

    config = load_config()
    timezone_name = qth_timezone_name(
        config.station.latitude_deg,
        config.station.longitude_deg,
    )
    positions: list[dict[str, object]] = []
    for satellite in satellites:
        try:
            position = engine.get_position(satellite.norad_id)
        except KeyError:
            continue
        positions.append(
            {
                "norad_id": satellite.norad_id,
                "satellite_name": satellite.name,
                "latitude_deg": position.latitude_deg,
                "longitude_deg": position.longitude_deg,
                "azimuth_deg": position.azimuth_deg,
                "elevation_deg": position.elevation_deg,
            }
        )

    return {
        "timezone": timezone_name,
        "positions": positions,
    }


@app.get("/api/tracking/ground-track")
def get_tracking_ground_track(
    norad_id: int = Query(..., ge=1),
    minutes_before: int = Query(default=45, ge=0, le=240),
    minutes_after: int = Query(default=45, ge=1, le=240),
    step_seconds: int = Query(default=60, ge=5, le=300),
) -> dict[str, object]:
    try:
        engine = _build_orbital_engine()
    except HTTPException:
        return {"norad_id": norad_id, "points": []}

    now_utc = datetime.now(timezone.utc)
    start_utc = now_utc - timedelta(minutes=minutes_before)
    end_utc = now_utc + timedelta(minutes=minutes_after)
    try:
        points = engine.get_ground_track(
            norad_id=norad_id,
            start_utc=start_utc,
            end_utc=end_utc,
            step_seconds=step_seconds,
        )
    except KeyError:
        points = []
    return {
        "norad_id": norad_id,
        "start_utc": start_utc.isoformat(),
        "end_utc": end_utc.isoformat(),
        "points": points,
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


def _payload_norad_id(payload: dict[str, Any]) -> int | None:
    raw_norad = payload.get("norad_id")
    return int(raw_norad) if raw_norad else None


def _payload_frequency_profile_index(payload: dict[str, Any]) -> int:
    raw_profile_index = payload.get("frequency_profile_index")
    return int(raw_profile_index) if raw_profile_index else 0


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

    _shutdown_runtime()
    _load_hamlib_model_caches()
    config = load_config()
    shared_local_client = None
    if _uses_same_local_radio(config):
        shared_local_client = LocalHamlibClient(
            model_id=config.rx.model_id or config.tx.model_id or 0,
            serial_port=config.rx.serial_port or config.tx.serial_port,
            baud=config.rx.baud or config.tx.baud or 0,
            timeout_s=max(config.rx.timeout_s, config.tx.timeout_s),
            debug_logging=bool(config.rx.cat_debug_logging or config.tx.cat_debug_logging),
        )
    if config.rx.enabled:
        sdr_manager = _build_rx_manager(config.rx, shared_local_client)
        sdr_manager.start()
    if config.tx.enabled:
        tx_radio_manager = RadioManager(
            client=_build_radio_client(config.tx, "TX", shared_local_client),
            enabled=config.tx.enabled,
            write_enabled=config.tx.write_enabled,
            target_vfo=config.tx.target_vfo,
        )
    if config.rotator.enabled:
        rotator_manager = RotatorManager(
            client=_build_rotator_client(config.rotator),
            enabled=config.rotator.enabled,
            write_enabled=config.rotator.write_enabled,
            min_elevation_deg=config.rotator.min_elevation_deg or 0.0,
            home_azimuth_deg=config.rotator.home_azimuth_deg or 0.0,
            home_elevation_deg=config.rotator.home_elevation_deg or 0.0,
            return_home_after_pass=config.rotator.return_home_after_pass,
        )
    try:
        _ensure_pass_cache()
    except Exception:
        LOGGER.exception("TLE/pass cache unavailable during runtime reload; continuing startup")


def _reload_rotator_config_only() -> None:
    global rotator_manager, rx_tracking_manager

    config = load_config()
    if rotator_manager is not None and hasattr(rotator_manager.client, "close"):
        rotator_manager.client.close()
    rotator_manager = None
    if config.rotator.enabled:
        rotator_manager = RotatorManager(
            client=_build_rotator_client(config.rotator),
            enabled=config.rotator.enabled,
            write_enabled=config.rotator.write_enabled,
            min_elevation_deg=config.rotator.min_elevation_deg or 0.0,
            home_azimuth_deg=config.rotator.home_azimuth_deg or 0.0,
            home_elevation_deg=config.rotator.home_elevation_deg or 0.0,
            return_home_after_pass=config.rotator.return_home_after_pass,
        )
    if rx_tracking_manager is not None:
        rx_tracking_manager.rotator_manager = rotator_manager


def _build_rx_manager(device_config, shared_local_client=None):
    if device_config.connectivity == "network":
        return PollingSdrManager(
            host=device_config.host,
            port=device_config.port,
            timeout_s=device_config.timeout_s,
            poll_interval_s=1.0,
        )
    if device_config.connectivity == "local":
        client = _build_radio_client(device_config, "RX", shared_local_client)
        return PollingRadioFrequencyManager(
            radio_manager=RadioManager(
                client=client,
                enabled=device_config.enabled,
                write_enabled=device_config.write_enabled,
                target_vfo=device_config.target_vfo,
            ),
            poll_interval_s=1.0,
        )
    raise ValueError(f"Unsupported RX connectivity: {device_config.connectivity}")


def _build_radio_client(device_config, role: str, shared_local_client=None):
    if device_config.connectivity == "network":
        return HamlibClient(
            host=device_config.host,
            port=device_config.port,
            timeout_s=device_config.timeout_s,
            target_vfo=device_config.target_vfo,
        )
    if device_config.connectivity == "local":
        if not device_config.model_id:
            raise ValueError(f"{role} model_id is required for local CAT control")
        if not device_config.serial_port:
            raise ValueError(f"{role} serial_port is required for local CAT control")
        if not device_config.baud:
            raise ValueError(f"{role} baud is required for local CAT control")
        if shared_local_client is not None:
            return shared_local_client
        return LocalHamlibClient(
            model_id=device_config.model_id,
            serial_port=device_config.serial_port,
            baud=device_config.baud,
            timeout_s=device_config.timeout_s,
            target_vfo=device_config.target_vfo,
            debug_logging=device_config.cat_debug_logging,
        )
    raise ValueError(f"Unsupported TX connectivity: {device_config.connectivity}")


def _build_rotator_client(device_config):
    if device_config.connectivity == "network":
        return RotctldClient(
            host=device_config.host,
            port=device_config.port,
            timeout_s=device_config.timeout_s,
        )
    if device_config.connectivity == "local":
        if not device_config.model_id:
            raise ValueError("Rotator model_id is required for local control")
        if not device_config.serial_port:
            raise ValueError("Rotator serial_port is required for local control")
        if not device_config.baud:
            raise ValueError("Rotator baud is required for local control")
        return LocalRotctldClient(
            model_id=device_config.model_id,
            serial_port=device_config.serial_port,
            baud=device_config.baud,
            timeout_s=device_config.timeout_s,
        )
    raise ValueError(f"Unsupported rotator connectivity: {device_config.connectivity}")


def _load_hamlib_model_caches() -> None:
    global hamlib_radio_models_cache, hamlib_radio_models_error
    global hamlib_rotator_models_cache, hamlib_rotator_models_error

    try:
        hamlib_radio_models_cache = [
            model.to_dict() for model in load_hamlib_radio_models()
        ]
        hamlib_radio_models_error = None
    except FileNotFoundError:
        hamlib_radio_models_cache = []
        hamlib_radio_models_error = "rigctl was not found on this system."
    except Exception as exc:
        hamlib_radio_models_cache = []
        hamlib_radio_models_error = str(exc)

    try:
        hamlib_rotator_models_cache = [
            model.to_dict() for model in load_hamlib_rotator_models()
        ]
        hamlib_rotator_models_error = None
    except FileNotFoundError:
        hamlib_rotator_models_cache = []
        hamlib_rotator_models_error = "rotctl was not found on this system."
    except Exception as exc:
        hamlib_rotator_models_cache = []
        hamlib_rotator_models_error = str(exc)


def _uses_same_local_radio(config) -> bool:
    return (
        config.rx.enabled
        and config.tx.enabled
        and config.rx.connectivity == "local"
        and config.tx.connectivity == "local"
        and bool(config.rx.serial_port)
        and config.rx.serial_port == config.tx.serial_port
        and config.rx.model_id == config.tx.model_id
        and config.rx.baud == config.tx.baud
    )


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


def _pass_to_dict(satellite_pass: SatellitePass) -> dict[str, object]:
    config = load_config()
    timezone_name = qth_timezone_name(
        config.station.latitude_deg,
        config.station.longitude_deg,
    )
    return {
        "satellite_name": satellite_pass.satellite_name,
        "norad_id": satellite_pass.norad_id,
        "aos_utc": satellite_pass.aos_utc.isoformat(),
        "max_utc": satellite_pass.max_utc.isoformat(),
        "los_utc": satellite_pass.los_utc.isoformat(),
        "start_azimuth_deg": satellite_pass.start_azimuth_deg,
        "middle_azimuth_deg": satellite_pass.middle_azimuth_deg,
        "end_azimuth_deg": satellite_pass.end_azimuth_deg,
        "aos_local": to_local_iso(satellite_pass.aos_utc, timezone_name),
        "max_local": to_local_iso(satellite_pass.max_utc, timezone_name),
        "los_local": to_local_iso(satellite_pass.los_utc, timezone_name),
        "aos_local_label": to_local_label(satellite_pass.aos_utc, timezone_name),
        "max_local_label": to_local_label(satellite_pass.max_utc, timezone_name),
        "los_local_label": to_local_label(satellite_pass.los_utc, timezone_name),
        "timezone": timezone_name,
        "max_elevation_deg": satellite_pass.max_elevation_deg,
    }


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


frontend_dir = PROJECT_ROOT / "frontend"
if frontend_dir.exists():
    app.mount("/", ConfigurableStaticFiles(directory=frontend_dir, html=True), name="frontend")
