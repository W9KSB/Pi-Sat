from __future__ import annotations

import json
import os
from dataclasses import replace
from pathlib import Path
from tempfile import NamedTemporaryFile
from threading import RLock

from pi_sat_controller.backend.models import SatelliteProfile, TransponderProfile

_PROFILE_LOCK = RLock()


def load_satellite_profiles(path: Path | str) -> list[SatelliteProfile]:
    with _PROFILE_LOCK:
        with Path(path).open("r", encoding="utf-8") as profile_file:
            raw_profiles = json.load(profile_file)

    return [
        SatelliteProfile(
            name=profile["name"],
            norad_id=int(profile["norad_id"]),
            favorite=bool(profile.get("favorite", False)),
            last_transponder_name=profile.get("last_transponder_name"),
            transponders=[
                TransponderProfile(
                    name=transponder["name"],
                    type=transponder["type"],
                    uplink_low=int(transponder["uplink_low"]),
                    uplink_high=int(transponder["uplink_high"]),
                    downlink_low=int(transponder["downlink_low"]),
                    downlink_high=int(transponder["downlink_high"]),
                    uplink_mode=transponder["uplink_mode"],
                    downlink_mode=transponder["downlink_mode"],
                    inverted=bool(transponder["inverted"]),
                    ratio=float(transponder.get("ratio", 1.0)),
                    preferred_uplink=int(transponder["preferred_uplink"]),
                    preferred_downlink=int(transponder["preferred_downlink"]),
                    tone=transponder.get("tone"),
                    custom=bool(transponder.get("custom", False)),
                )
                for transponder in profile.get("transponders", [])
            ],
        )
        for profile in raw_profiles
    ]


def save_satellite_profiles(
    path: Path | str,
    profiles: list[SatelliteProfile],
) -> None:
    payload = [
        {
            "name": profile.name,
            "norad_id": profile.norad_id,
            "favorite": profile.favorite,
            "last_transponder_name": profile.last_transponder_name,
            "transponders": [
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
                    "custom": transponder.custom,
                }
                for transponder in profile.transponders
            ],
        }
        for profile in profiles
    ]
    target = Path(path).resolve()
    temporary_path: Path | None = None
    with _PROFILE_LOCK:
        try:
            with NamedTemporaryFile(
                "w",
                encoding="utf-8",
                dir=target.parent,
                prefix=f".{target.name}.",
                suffix=".tmp",
                delete=False,
            ) as temporary:
                json.dump(payload, temporary, indent=2)
                temporary.write("\n")
                temporary.flush()
                os.fsync(temporary.fileno())
                temporary_path = Path(temporary.name)
            os.replace(temporary_path, target)
            temporary_path = None
        finally:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)


def upsert_satellite_transponders(
    path: Path | str,
    satellite: SatelliteProfile,
) -> SatelliteProfile:
    with _PROFILE_LOCK:
        profiles = load_satellite_profiles(path)
        updated_profiles: list[SatelliteProfile] = []
        replaced = False
        for existing in profiles:
            if existing.norad_id == satellite.norad_id:
                custom = [item for item in existing.transponders if item.custom]
                custom_names = {item.name.casefold() for item in custom}
                satellite = replace(
                    satellite,
                    transponders=[
                        item for item in satellite.transponders
                        if not item.custom and item.name.casefold() not in custom_names
                    ] + custom,
                    last_transponder_name=existing.last_transponder_name,
                )
                updated_profiles.append(satellite)
                replaced = True
            else:
                updated_profiles.append(existing)
        if not replaced:
            updated_profiles.append(satellite)
        save_satellite_profiles(path, updated_profiles)
    return satellite


def add_custom_transponder(
    path: Path | str,
    satellite: SatelliteProfile,
    transponder: TransponderProfile,
) -> SatelliteProfile:
    with _PROFILE_LOCK:
        profiles = load_satellite_profiles(path)
        index = next(
            (index for index, item in enumerate(profiles) if item.norad_id == satellite.norad_id),
            None,
        )
        if index is not None:
            satellite = profiles[index]
        if any(item.name.casefold() == transponder.name.casefold() for item in satellite.transponders):
            raise ValueError("A frequency profile with this name already exists for this satellite")
        satellite = replace(satellite, transponders=[*satellite.transponders, transponder])
        if index is None:
            profiles.append(satellite)
        else:
            profiles[index] = satellite
        save_satellite_profiles(path, profiles)
        return satellite


def preferred_transponder_index(satellite: SatelliteProfile) -> int:
    return next(
        (
            index
            for index, transponder in enumerate(satellite.transponders)
            if transponder.name == satellite.last_transponder_name
        ),
        0,
    )


def remember_satellite_transponder(
    path: Path | str, norad_id: int, transponder_name: str
) -> None:
    with _PROFILE_LOCK:
        profiles = load_satellite_profiles(path)
        for index, satellite in enumerate(profiles):
            if satellite.norad_id != norad_id:
                continue
            if not any(item.name == transponder_name for item in satellite.transponders):
                raise ValueError("Selected frequency profile is invalid")
            if satellite.last_transponder_name != transponder_name:
                profiles[index] = replace(
                    satellite, last_transponder_name=transponder_name
                )
                save_satellite_profiles(path, profiles)
            return
        raise ValueError(f"NORAD {norad_id} has no local frequency profile")
