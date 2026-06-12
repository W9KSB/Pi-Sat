from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from threading import Lock
from typing import Any

from fastapi import Body, FastAPI, HTTPException, Query

from pi_sat_controller.backend.config import load_config
from pi_sat_controller.backend.models import MySatellite, SatelliteProfile
from pi_sat_controller.backend.satellites.satellite_profiles import (
    load_satellite_profiles,
    upsert_satellite_transponders,
)
from pi_sat_controller.backend.satellites.tle_manager import TleManager
from pi_sat_controller.backend.satellites.transponder_source_client import (
    TransponderSourceClient,
)


def register_satellites_api(
    app: FastAPI,
    *,
    logger,
    serialize_frequency_profiles: Callable[[list[Any]], list[dict[str, object]]],
    qth_timezone_from_config: Callable[[], str],
    build_orbital_engine: Callable[[], Any],
    load_my_satellites: Callable[[], tuple[list[Any], float, bool]],
    save_my_satellites: Callable[[list[Any], float, bool], None],
    ensure_pass_cache: Callable[[], None],
    refresh_pass_cache: Callable[[bool], list[Any]],
    get_pass_cache: Callable[[], list[Any]],
    pass_cache_lock: Lock,
    pass_to_dict: Callable[[Any], dict[str, object]],
    get_pass_cache_refreshed_at_utc: Callable[[], str | None],
) -> None:
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
                "frequency_profiles": serialize_frequency_profiles(
                    profile_by_norad.get(
                        my_satellite.norad_id,
                        SatelliteProfile(
                            name=my_satellite.name,
                            norad_id=my_satellite.norad_id,
                            favorite=True,
                            transponders=[],
                        ),
                    ).transponders
                ),
            }
            for my_satellite in my_satellites
        ]

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
            "frequency_profiles": serialize_frequency_profiles(
                profile.transponders if profile else []
            ),
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
        logger.info(
            "Manual transponder refresh requested for %s (%s)",
            satellite_name,
            norad_id,
        )

        try:
            transponders = TransponderSourceClient().get_transponders(norad_id)
        except Exception as exc:
            logger.exception(
                "Manual transponder refresh failed for %s (%s)",
                satellite_name,
                norad_id,
            )
            raise HTTPException(
                status_code=502,
                detail=f"Frequency profile update failed: {exc}",
            ) from exc

        if not transponders:
            logger.warning(
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
        logger.info(
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
                {"name": transponder["name"]}
                for transponder in serialize_frequency_profiles(profile.transponders)
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

        engine = build_orbital_engine()
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
            ensure_pass_cache()
        except Exception:
            logger.exception("Pass cache refresh failed while serving /api/my-satellites/passes")
            return {"hours": hours, "passes": []}

        now_utc = datetime.now(timezone.utc)
        horizon_utc = now_utc + timedelta(hours=hours)
        satellites, _, _ = load_my_satellites()
        selected_norad_ids = {satellite.norad_id for satellite in satellites}

        grouped: dict[int, list[dict[str, object]]] = {norad_id: [] for norad_id in selected_norad_ids}
        with pass_cache_lock:
            cached = list(get_pass_cache())

        for satellite_pass in cached:
            if satellite_pass.norad_id not in selected_norad_ids:
                continue
            if satellite_pass.los_utc <= now_utc:
                continue
            if satellite_pass.aos_utc > horizon_utc:
                continue
            grouped.setdefault(satellite_pass.norad_id, []).append(pass_to_dict(satellite_pass))

        return {
            "hours": hours,
            "passes": [
                {"norad_id": norad_id, "passes": grouped.get(norad_id, [])}
                for norad_id in sorted(grouped)
            ],
        }

    @app.post("/api/passes/refresh")
    def refresh_passes() -> dict[str, object]:
        refreshed = refresh_pass_cache(force_tle_download=True)
        return {
            "ok": True,
            "refreshed_at_utc": get_pass_cache_refreshed_at_utc(),
            "pass_count": len(refreshed),
        }

    @app.post("/api/tle/refresh")
    def refresh_tle_data() -> dict[str, object]:
        config = load_config()
        tle_manager = TleManager(config.tle.source_url, config.tle.cache_dir)
        logger.info("Manual TLE refresh requested")
        try:
            status = tle_manager.download()
        except Exception as exc:
            logger.exception("Manual TLE refresh failed")
            raise HTTPException(status_code=502, detail=f"TLE refresh failed: {exc}") from exc
        refresh_pass_cache(force_tle_download=False)
        logger.info("Manual TLE refresh completed at %s", status.downloaded_at_utc)
        return {
            "ok": True,
            "refreshed_at_utc": status.downloaded_at_utc.isoformat() if status.downloaded_at_utc else None,
        }

    @app.get("/api/passes/next")
    def get_next_passes(
        norad_ids: str | None = Query(default=None),
    ) -> list[dict[str, object]]:
        try:
            ensure_pass_cache()
        except Exception:
            logger.exception("Pass cache refresh failed while serving /api/passes/next")
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
            cached = list(get_pass_cache())
        passes = [
            satellite_pass
            for satellite_pass in cached
            if satellite_pass.norad_id in selected_norad_ids
            and satellite_pass.los_utc > now_utc
        ][:15]
        return [pass_to_dict(satellite_pass) for satellite_pass in passes]

    @app.get("/api/tracked-satellites/positions")
    def get_tracked_satellite_positions(
        norad_ids: str | None = Query(default=None),
    ) -> dict[str, object]:
        try:
            engine = build_orbital_engine()
        except HTTPException:
            return {
                "timezone": qth_timezone_from_config(),
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

        timezone_name = qth_timezone_from_config()
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
