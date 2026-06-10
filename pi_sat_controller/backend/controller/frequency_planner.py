from __future__ import annotations

from pi_sat_controller.backend.models import FrequencyPlan, TransponderProfile


def map_downlink_offset_to_uplink(
    user_downlink_offset_hz: int,
    transponder: TransponderProfile,
) -> int:
    """Map manual downlink tuning into the matching uplink correction.

    Inverting linear transponders reverse the direction of user tuning.
    Doppler offsets are deliberately handled separately from manual passband
    offsets so each band can receive its own frequency-dependent correction.
    """
    direction = -1 if transponder.inverted else 1
    return round(direction * user_downlink_offset_hz * transponder.ratio)


def map_uplink_offset_to_downlink(
    user_uplink_offset_hz: int,
    transponder: TransponderProfile,
) -> int:
    direction = -1 if transponder.inverted else 1
    return round(direction * user_uplink_offset_hz / transponder.ratio)


def plan_from_offsets(
    transponder: TransponderProfile,
    user_downlink_offset_hz: int,
    user_uplink_offset_hz: int,
    downlink_doppler_hz: int = 0,
    uplink_doppler_hz: int = 0,
) -> FrequencyPlan:
    return FrequencyPlan(
        downlink_hz=(
            transponder.preferred_downlink
            + downlink_doppler_hz
            + user_downlink_offset_hz
        ),
        uplink_hz=(
            transponder.preferred_uplink + uplink_doppler_hz + user_uplink_offset_hz
        ),
        user_downlink_offset_hz=user_downlink_offset_hz,
        mapped_user_uplink_offset_hz=user_uplink_offset_hz,
        downlink_doppler_hz=downlink_doppler_hz,
        uplink_doppler_hz=uplink_doppler_hz,
    )


def plan_from_downlink_offset(
    transponder: TransponderProfile,
    user_downlink_offset_hz: int,
    downlink_doppler_hz: int = 0,
    uplink_doppler_hz: int = 0,
) -> FrequencyPlan:
    mapped_uplink_offset = map_downlink_offset_to_uplink(
        user_downlink_offset_hz, transponder
    )
    return plan_from_offsets(
        transponder=transponder,
        user_downlink_offset_hz=user_downlink_offset_hz,
        user_uplink_offset_hz=mapped_uplink_offset,
        downlink_doppler_hz=downlink_doppler_hz,
        uplink_doppler_hz=uplink_doppler_hz,
    )
