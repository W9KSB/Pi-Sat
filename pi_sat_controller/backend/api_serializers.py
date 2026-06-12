from __future__ import annotations

from typing import Any

from pi_sat_controller.backend.config import load_config
from pi_sat_controller.backend.models import SatellitePass
from pi_sat_controller.backend.timezone_utils import (
    qth_timezone_name,
    to_local_iso,
    to_local_label,
)


def serialize_transponder(transponder) -> dict[str, object]:
    return {
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


def serialize_frequency_profiles(transponders) -> list[dict[str, object]]:
    return [serialize_transponder(transponder) for transponder in transponders]


def qth_timezone_from_config() -> str:
    config = load_config()
    return qth_timezone_name(
        config.station.latitude_deg,
        config.station.longitude_deg,
    )


def serialize_pass(
    satellite_pass: SatellitePass,
    timezone_name: str,
) -> dict[str, Any]:
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


def payload_norad_id(payload: dict[str, Any]) -> int | None:
    raw_norad = payload.get("norad_id")
    return int(raw_norad) if raw_norad else None


def payload_frequency_profile_index(payload: dict[str, Any]) -> int:
    raw_profile_index = payload.get("frequency_profile_index")
    return int(raw_profile_index) if raw_profile_index else 0
