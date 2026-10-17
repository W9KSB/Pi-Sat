from __future__ import annotations

"""Passive station monitor snapshots from the shared runtime and local orbit data."""

from collections.abc import Callable
from datetime import datetime, timedelta, timezone
import math
from pathlib import Path
from threading import Lock
from typing import Any

from fastapi import FastAPI
from fastapi.responses import FileResponse

from pi_sat_controller.backend.config import NATIVE_ICOM_DEVICE_ID
from pi_sat_controller.backend.orbital.skyfield_engine import SkyfieldEngine
from pi_sat_controller.backend.radio.radio_manager import RadioManager


def _device_snapshot(manager: Any) -> dict[str, Any]:
    if manager is None:
        return {}
    radio = getattr(manager, "radio_manager", manager)
    if isinstance(radio, RadioManager):
        # The normal radio snapshot drains async events for control-side
        # reconciliation. A monitor must leave that queue with its owner.
        with radio._lock:
            return {
                "connected": radio._connected,
                "frequency_hz": radio._frequency_hz,
                "last_read_at_utc": radio._last_read_at_utc,
            }
    snapshot = manager.snapshot()
    return snapshot if isinstance(snapshot, dict) else snapshot.to_dict()


def register_dashboard_api(
    app: FastAPI,
    *,
    frontend_dir: Path,
    get_config: Callable[[], Any],
    load_my_satellites: Callable[[], tuple[list[Any], float, bool, set[int]]],
    get_pass_cache: Callable[[], list[Any]],
    pass_cache_lock: Lock,
    get_pass_cache_refreshed_at_utc: Callable[[], str | None],
    pass_to_dict: Callable[[Any], dict[str, Any]],
    qth_timezone_from_config: Callable[[], str],
    get_sdr_manager: Callable[[], Any],
    get_tx_radio_manager: Callable[[], Any],
    get_rx_tracking_manager: Callable[[], Any],
    get_rotator_manager: Callable[[], Any],
    get_icom_controller: Callable[[], Any],
) -> None:
    orbit_lock = Lock()
    orbit_key: tuple[Any, ...] | None = None
    orbit_engine: SkyfieldEngine | None = None

    def local_engine(config: Any) -> SkyfieldEngine | None:
        nonlocal orbit_key, orbit_engine
        cache_file = config.tle.cache_dir / "tle_cache.tle"
        try:
            stat = cache_file.stat()
            key = (cache_file, stat.st_mtime_ns, stat.st_size,
                   config.station.latitude_deg, config.station.longitude_deg,
                   config.station.elevation_m)
            with orbit_lock:
                if key != orbit_key:
                    engine = SkyfieldEngine(
                        tle_file=cache_file,
                        latitude_deg=config.station.latitude_deg,
                        longitude_deg=config.station.longitude_deg,
                        elevation_m=config.station.elevation_m,
                    )
                    orbit_engine, orbit_key = engine, key
                return orbit_engine
        except (OSError, ValueError, TypeError, KeyError):
            return None

    @app.get("/dashboard", include_in_schema=False)
    @app.get("/dashboard/", include_in_schema=False)
    def dashboard_page() -> FileResponse:
        try:
            caching_enabled = get_config().server.gui_resources_caching
        except Exception:
            caching_enabled = False
        headers = {} if caching_enabled else {
            "Cache-Control": "no-store, no-cache, must-revalidate",
            "Pragma": "no-cache", "Expires": "0",
        }
        return FileResponse(frontend_dir / "dashboard.html", headers=headers)

    @app.get("/api/dashboard")
    def dashboard_snapshot() -> dict[str, Any]:
        config = get_config()
        now = datetime.now(timezone.utc)
        horizon = now + timedelta(hours=24)
        timezone_name = qth_timezone_from_config()
        satellites, _, _, checked_ids = load_my_satellites()
        satellites = [satellite for satellite in satellites if satellite.norad_id in checked_ids]
        selected_ids = {satellite.norad_id for satellite in satellites}
        with pass_cache_lock:
            cached_passes = list(get_pass_cache())
            refreshed_at = get_pass_cache_refreshed_at_utc()
        remaining = sorted(
            (item for item in cached_passes if item.norad_id in selected_ids
             and item.los_utc > now and item.aos_utc <= horizon),
            key=lambda item: item.aos_utc,
        )
        cache_stale = True
        if refreshed_at:
            try:
                cache_time = datetime.fromisoformat(refreshed_at)
                cache_stale = now - cache_time >= timedelta(hours=6)
            except (ValueError, TypeError):
                pass

        positions = []
        engine = local_engine(config) if satellites else None
        for satellite in satellites:
            if engine is None:
                break
            try:
                position = engine.get_position_at(satellite.norad_id, now)
                if not all(math.isfinite(value) for value in (
                    position.latitude_deg, position.longitude_deg,
                    position.azimuth_deg, position.elevation_deg,
                )):
                    continue
                track = engine.get_ground_track(
                    satellite.norad_id, now - timedelta(minutes=45),
                    now + timedelta(minutes=45), step_seconds=180,
                )
            except (KeyError, ValueError, TypeError):
                continue
            positions.append({
                "norad_id": satellite.norad_id, "satellite_name": satellite.name,
                "latitude_deg": position.latitude_deg,
                "longitude_deg": position.longitude_deg,
                "azimuth_deg": position.azimuth_deg,
                "elevation_deg": position.elevation_deg,
                "ground_track": [point for point in track if
                                 math.isfinite(point["latitude_deg"])
                                 and math.isfinite(point["longitude_deg"])],
            })

        tracking_manager = get_rx_tracking_manager()
        tracking = _device_snapshot(tracking_manager)
        rx = {} if config.rx.device_id == NATIVE_ICOM_DEVICE_ID else _device_snapshot(get_sdr_manager())
        tx = {} if config.tx.device_id == NATIVE_ICOM_DEVICE_ID else _device_snapshot(get_tx_radio_manager())
        rotator = _device_snapshot(get_rotator_manager())
        controller = get_icom_controller()
        native = controller.try_snapshot() if controller is not None else {}

        def role_status(role: str, snapshot: dict[str, Any]) -> dict[str, Any]:
            device = getattr(config, role)
            native_role = device.device_id == NATIVE_ICOM_DEVICE_ID
            cat_device = config.cat_devices.get(device.device_id)
            name = "IC-9700" if native_role else (
                cat_device.name if cat_device is not None else "Not configured"
            )
            side = native.get("sub" if role == "rx" else "main", {})
            target_hz = tracking.get("target_rx_hz" if role == "rx" else "calculated_tx_hz")
            return {
                "name": name, "enabled": device.enabled,
                "connected": bool(native.get("connected") if native_role
                                  else snapshot.get("connected")),
                "active": bool(device.enabled and tracking.get("active")
                               and (role == "rx" or target_hz is not None)),
                "frequency_hz": side.get("frequency_hz") if native_role else snapshot.get("frequency_hz"),
                "target_frequency_hz": target_hz,
                "last_read_at_utc": snapshot.get("last_read_at_utc"),
            }

        rx_status, tx_status = role_status("rx", rx), role_status("tx", tx)
        radio_names = list(dict.fromkeys(item["name"] for item in (rx_status, tx_status)
                                       if item["name"] != "Not configured"))
        return {
            "generated_at_utc": now.isoformat(), "timezone": timezone_name,
            "qth": {"name": config.station.name,
                    "latitude_deg": config.station.latitude_deg,
                    "longitude_deg": config.station.longitude_deg},
            "satellites": [{"norad_id": item.norad_id, "name": item.name} for item in satellites],
            "positions": positions,
            "positions_available": not satellites or bool(positions),
            "passes": [pass_to_dict(item) for item in remaining],
            "pass_cache_refreshed_at_utc": refreshed_at,
            "pass_cache_available": bool(refreshed_at or cached_passes),
            "pass_cache_stale": cache_stale,
            "status": {
                "radio": {"name": " / ".join(radio_names) or "Not configured",
                          "connected": rx_status["connected"] or tx_status["connected"]},
                "rx": rx_status, "tx": tx_status,
                "rotator": {"enabled": config.rotator.enabled,
                            "connected": bool(rotator.get("connected")),
                            "active": bool(config.rotator.enabled and rotator.get("write_enabled")
                                           and rotator.get("pass_active")),
                            "azimuth_deg": rotator.get("current_azimuth_deg"),
                            "elevation_deg": rotator.get("current_elevation_deg"),
                            "last_read_at_utc": rotator.get("last_read_at_utc")},
                "target": {"norad_id": tracking.get("norad_id"),
                           "name": tracking.get("satellite_name"),
                           "pass_active": bool(tracking.get("pass_active")),
                           "last_update_at_utc": tracking.get("last_update_at_utc")},
            },
        }
