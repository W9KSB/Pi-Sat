"""Skyfield-backed orbital engine.

Skyfield-specific imports should stay in this module.
"""

from __future__ import annotations

import math
from pathlib import Path
from datetime import datetime, timedelta, timezone

from skyfield.api import EarthSatellite, load, wgs84

from pi_sat_controller.backend.models import SatellitePass
from pi_sat_controller.backend.orbital.orbital_engine import (
    OrbitalEngine,
    SatellitePosition,
)


class SkyfieldEngine(OrbitalEngine):
    def __init__(
        self,
        tle_file: Path,
        latitude_deg: float,
        longitude_deg: float,
        elevation_m: float,
    ) -> None:
        self.tle_file = tle_file
        self.timescale = load.timescale()
        self.observer = wgs84.latlon(
            latitude_degrees=latitude_deg,
            longitude_degrees=longitude_deg,
            elevation_m=elevation_m,
        )
        self.satellites = self._load_satellites(tle_file)

    def get_position(self, norad_id: int) -> SatellitePosition:
        return self.get_position_at(norad_id, datetime.now(timezone.utc))

    def get_position_at(
        self,
        norad_id: int,
        at_utc: datetime,
    ) -> SatellitePosition:
        satellite = self.satellites.get(norad_id)
        if satellite is None:
            raise KeyError(f"NORAD {norad_id} not found in {self.tle_file}")

        instant = self.timescale.from_datetime(at_utc.astimezone(timezone.utc))
        geocentric = satellite.at(instant)
        subpoint = wgs84.subpoint(geocentric)
        position = (satellite - self.observer).at(instant)
        altitude, azimuth, distance = position.altaz()
        _, _, _, _, _, range_rate = position.frame_latlon_and_rates(self.observer)

        return SatellitePosition(
            azimuth_deg=azimuth.degrees,
            elevation_deg=altitude.degrees,
            latitude_deg=subpoint.latitude.degrees,
            longitude_deg=subpoint.longitude.degrees,
            range_km=distance.km,
            range_rate_m_s=range_rate.km_per_s * 1000,
        )

    def get_visibility_footprint(
        self,
        norad_id: int,
        at_utc: datetime,
        point_count: int = 72,
    ) -> list[dict[str, float]]:
        satellite = self.satellites.get(norad_id)
        if satellite is None:
            raise KeyError(f"NORAD {norad_id} not found in {self.tle_file}")

        instant = self.timescale.from_datetime(at_utc.astimezone(timezone.utc))
        geocentric = satellite.at(instant)
        subpoint = wgs84.subpoint(geocentric)
        altitude_km = max(0.0, subpoint.elevation.km)
        earth_radius_km = 6371.0
        if altitude_km <= 0.0:
            return []

        angular_distance_rad = math.acos(earth_radius_km / (earth_radius_km + altitude_km))
        center_lat_rad = math.radians(subpoint.latitude.degrees)
        center_lon_rad = math.radians(subpoint.longitude.degrees)
        total_points = max(24, point_count)
        points: list[dict[str, float]] = []

        for index in range(total_points + 1):
            bearing_rad = (math.tau * index) / total_points
            latitude_rad = math.asin(
                math.sin(center_lat_rad) * math.cos(angular_distance_rad)
                + math.cos(center_lat_rad) * math.sin(angular_distance_rad) * math.cos(bearing_rad)
            )
            longitude_rad = center_lon_rad + math.atan2(
                math.sin(bearing_rad) * math.sin(angular_distance_rad) * math.cos(center_lat_rad),
                math.cos(angular_distance_rad) - math.sin(center_lat_rad) * math.sin(latitude_rad),
            )
            longitude_deg = ((math.degrees(longitude_rad) + 180) % 360) - 180
            points.append(
                {
                    "latitude_deg": round(math.degrees(latitude_rad), 5),
                    "longitude_deg": round(longitude_deg, 5),
                }
            )

        return points

    def get_ground_track(
        self,
        norad_id: int,
        start_utc: datetime,
        end_utc: datetime,
        step_seconds: int = 60,
    ) -> list[dict[str, float]]:
        satellite = self.satellites.get(norad_id)
        if satellite is None:
            raise KeyError(f"NORAD {norad_id} not found in {self.tle_file}")
        if step_seconds < 1:
            step_seconds = 1
        if end_utc <= start_utc:
            return []

        points: list[dict[str, float]] = []
        cursor = start_utc
        while cursor <= end_utc:
            at_time = self.timescale.from_datetime(cursor)
            geocentric = satellite.at(at_time)
            subpoint = wgs84.subpoint(geocentric)
            points.append(
                {
                    "latitude_deg": round(subpoint.latitude.degrees, 5),
                    "longitude_deg": round(subpoint.longitude.degrees, 5),
                }
            )
            cursor += timedelta(seconds=step_seconds)
        return points

    def get_passes(
        self,
        norad_id: int,
        satellite_name: str,
        min_elevation_deg: float,
        limit: int = 5,
        days_ahead: int = 7,
    ) -> list[SatellitePass]:
        satellite = self.satellites.get(norad_id)
        if satellite is None:
            raise KeyError(f"NORAD {norad_id} not found in {self.tle_file}")

        start = datetime.now(timezone.utc)
        end = start + timedelta(days=days_ahead)
        times, events = satellite.find_events(
            self.observer,
            self.timescale.from_datetime(start),
            self.timescale.from_datetime(end),
            altitude_degrees=0.0,
        )

        passes: list[SatellitePass] = []
        index = 0
        while index <= len(events) - 3 and len(passes) < limit:
            if list(events[index : index + 3]) != [0, 1, 2]:
                index += 1
                continue

            aos = times[index]
            maximum = times[index + 1]
            los = times[index + 2]
            _, start_azimuth, _ = (satellite - self.observer).at(aos).altaz()
            middle_altitude, middle_azimuth, _ = (satellite - self.observer).at(maximum).altaz()
            _, end_azimuth, _ = (satellite - self.observer).at(los).altaz()
            if middle_altitude.degrees < min_elevation_deg:
                index += 3
                continue
            passes.append(
                SatellitePass(
                    satellite_name=satellite_name,
                    norad_id=norad_id,
                    aos_utc=aos.utc_datetime(),
                    max_utc=maximum.utc_datetime(),
                    los_utc=los.utc_datetime(),
                    start_azimuth_deg=round(start_azimuth.degrees, 1),
                    middle_azimuth_deg=round(middle_azimuth.degrees, 1),
                    end_azimuth_deg=round(end_azimuth.degrees, 1),
                    max_elevation_deg=round(middle_altitude.degrees, 1),
                )
            )
            index += 3

        return passes

    def _load_satellites(self, tle_file: Path) -> dict[int, EarthSatellite]:
        lines = [
            line.strip()
            for line in tle_file.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        satellites: dict[int, EarthSatellite] = {}

        for index in range(0, len(lines) - 2, 3):
            name = lines[index]
            line1 = lines[index + 1]
            line2 = lines[index + 2]
            if not line1.startswith("1 ") or not line2.startswith("2 "):
                continue

            satellite = EarthSatellite(line1, line2, name, self.timescale)
            satellites[satellite.model.satnum] = satellite

        return satellites
