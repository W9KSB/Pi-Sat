from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class SatellitePosition:
    azimuth_deg: float
    elevation_deg: float
    latitude_deg: float
    longitude_deg: float
    range_km: float
    range_rate_m_s: float


class OrbitalEngine:
    def get_position(self, norad_id: int) -> SatellitePosition:
        raise NotImplementedError
