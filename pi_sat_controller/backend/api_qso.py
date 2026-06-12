from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta, timezone
import math
from typing import Any

from fastapi import Body, FastAPI, HTTPException

from pi_sat_controller.backend.maidenhead import locator_to_lat_lon
from pi_sat_controller.backend.orbital.skyfield_engine import SkyfieldEngine
from pi_sat_controller.backend.timezone_utils import qth_timezone_name


def register_qso_api(
    app: FastAPI,
    *,
    resolve_tle_cache_file: Callable[[], Any],
    load_my_satellites: Callable[[], tuple[list[Any], float, bool]],
    build_qso_opportunities: Callable[..., list[dict[str, object]]],
) -> None:
    @app.post("/api/qso-finder/search")
    def search_qso_windows(payload: dict[str, Any] = Body(...)) -> dict[str, object]:
        grid_1 = str(payload.get("grid_1", "")).strip()
        grid_2 = str(payload.get("grid_2", "")).strip()
        satellite_filter = payload.get("norad_id")
        min_elevation_deg = float(payload.get("min_elevation_deg", 10.0))
        hours = int(payload.get("hours", 48))
        min_duration_minutes = float(payload.get("min_duration_minutes", 2.0))

        if not grid_1 or not grid_2:
            raise HTTPException(status_code=400, detail="Both grid locators are required.")
        if hours < 1 or hours > 168:
            raise HTTPException(status_code=400, detail="Hours must be between 1 and 168.")
        if min_elevation_deg < 0 or min_elevation_deg > 90:
            raise HTTPException(status_code=400, detail="Minimum elevation must be between 0 and 90 degrees.")
        if min_duration_minutes < 0 or min_duration_minutes > 1440:
            raise HTTPException(status_code=400, detail="Minimum duration must be between 0 and 1440 minutes.")

        try:
            grid_1_lat, grid_1_lon = locator_to_lat_lon(grid_1)
            grid_2_lat, grid_2_lon = locator_to_lat_lon(grid_2)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        tle_file = resolve_tle_cache_file()
        engine_grid_1 = SkyfieldEngine(tle_file, grid_1_lat, grid_1_lon, 0.0)
        engine_grid_2 = SkyfieldEngine(tle_file, grid_2_lat, grid_2_lon, 0.0)
        now_utc = datetime.now(timezone.utc)
        horizon_utc = now_utc + timedelta(hours=hours)
        days_ahead = max(1, min(7, math.ceil(hours / 24) + 1))
        limit = max(48, hours * 3)
        grid_1_timezone = qth_timezone_name(grid_1_lat, grid_1_lon)
        grid_2_timezone = qth_timezone_name(grid_2_lat, grid_2_lon)

        satellites = load_my_satellites()[0]
        if satellite_filter not in (None, "", "all"):
            try:
                selected_norad = int(satellite_filter)
            except (TypeError, ValueError) as exc:
                raise HTTPException(status_code=400, detail="Satellite filter must be a NORAD ID or 'all'.") from exc
            satellites = [satellite for satellite in satellites if satellite.norad_id == selected_norad]

        opportunities: list[dict[str, object]] = []
        summary_options = [
            {
                "norad_id": satellite.norad_id,
                "name": satellite.name,
            }
            for satellite in satellites
        ]

        for satellite in satellites:
            try:
                grid_1_passes = [
                    satellite_pass
                    for satellite_pass in engine_grid_1.get_passes(
                        satellite.norad_id,
                        satellite.name,
                        min_elevation_deg=min_elevation_deg,
                        limit=limit,
                        days_ahead=days_ahead,
                    )
                    if satellite_pass.los_utc > now_utc and satellite_pass.aos_utc < horizon_utc
                ]
                grid_2_passes = [
                    satellite_pass
                    for satellite_pass in engine_grid_2.get_passes(
                        satellite.norad_id,
                        satellite.name,
                        min_elevation_deg=min_elevation_deg,
                        limit=limit,
                        days_ahead=days_ahead,
                    )
                    if satellite_pass.los_utc > now_utc and satellite_pass.aos_utc < horizon_utc
                ]
            except KeyError:
                continue

            opportunities.extend(
                build_qso_opportunities(
                    engine=engine_grid_1,
                    satellite_name=satellite.name,
                    norad_id=satellite.norad_id,
                    grid_1=grid_1,
                    grid_1_lat=grid_1_lat,
                    grid_1_lon=grid_1_lon,
                    grid_1_timezone=grid_1_timezone,
                    grid_1_passes=grid_1_passes,
                    grid_2=grid_2,
                    grid_2_lat=grid_2_lat,
                    grid_2_lon=grid_2_lon,
                    grid_2_timezone=grid_2_timezone,
                    grid_2_passes=grid_2_passes,
                    min_overlap_seconds=int(min_duration_minutes * 60),
                )
            )

        opportunities.sort(key=lambda item: item["overlap_start_utc"])
        return {
            "grid_1": {
                "locator": grid_1.upper(),
                "latitude_deg": grid_1_lat,
                "longitude_deg": grid_1_lon,
                "timezone": grid_1_timezone,
            },
            "grid_2": {
                "locator": grid_2.upper(),
                "latitude_deg": grid_2_lat,
                "longitude_deg": grid_2_lon,
                "timezone": grid_2_timezone,
            },
            "satellites": summary_options,
            "min_elevation_deg": min_elevation_deg,
            "min_duration_minutes": min_duration_minutes,
            "hours": hours,
            "opportunities": opportunities,
        }
