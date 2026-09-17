from __future__ import annotations

import json
import os
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
                updated_profiles.append(satellite)
                replaced = True
            else:
                updated_profiles.append(existing)
        if not replaced:
            updated_profiles.append(satellite)
        save_satellite_profiles(path, updated_profiles)
    return satellite
