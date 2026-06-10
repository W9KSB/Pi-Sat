from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum


class MasterMode(str, Enum):
    SDR_MASTER = "SDR_MASTER"
    RADIO_MASTER = "RADIO_MASTER"
    CONTROLLER_MASTER = "CONTROLLER_MASTER"
    MANUAL = "MANUAL"


@dataclass(frozen=True)
class TransponderProfile:
    name: str
    type: str
    uplink_low: int
    uplink_high: int
    downlink_low: int
    downlink_high: int
    uplink_mode: str
    downlink_mode: str
    inverted: bool
    ratio: float
    preferred_uplink: int
    preferred_downlink: int
    tone: float | None = None


@dataclass(frozen=True)
class SatelliteProfile:
    name: str
    norad_id: int
    favorite: bool
    transponders: list[TransponderProfile]


@dataclass(frozen=True)
class MySatellite:
    norad_id: int
    name: str


@dataclass(frozen=True)
class SatellitePass:
    satellite_name: str
    norad_id: int
    aos_utc: datetime
    max_utc: datetime
    los_utc: datetime
    start_azimuth_deg: float
    middle_azimuth_deg: float
    end_azimuth_deg: float
    max_elevation_deg: float


@dataclass(frozen=True)
class FrequencyPlan:
    downlink_hz: int
    uplink_hz: int | None
    user_downlink_offset_hz: int
    mapped_user_uplink_offset_hz: int | None
    downlink_doppler_hz: int = 0
    uplink_doppler_hz: int | None = 0
