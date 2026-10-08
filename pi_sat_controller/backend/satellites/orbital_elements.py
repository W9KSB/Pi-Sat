"""Parse and cache named TLEs and SGP4 GP/OMM JSON without converting elements."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import logging
import math
from typing import Any

from skyfield.api import EarthSatellite

LOGGER = logging.getLogger(__name__)
_CACHE_FORMAT = "pi-sat-orbital-elements"
_NUMERIC_FIELDS = (
    "MEAN_MOTION", "ECCENTRICITY", "INCLINATION", "RA_OF_ASC_NODE",
    "ARG_OF_PERICENTER", "MEAN_ANOMALY", "BSTAR",
    "MEAN_MOTION_DOT", "MEAN_MOTION_DDOT",
)


@dataclass(frozen=True)
class OrbitalElements:
    satellite: EarthSatellite
    tle_lines: tuple[str, str] | None = None
    omm_fields: dict[str, Any] | None = None

    @property
    def satnum(self) -> int:
        if self.omm_fields is not None:
            return self.omm_fields["NORAD_CAT_ID"]
        return self.satellite.model.satnum

    @property
    def epoch_utc(self) -> datetime:
        return self.satellite.epoch.utc_datetime().replace(tzinfo=timezone.utc)


def _parse_omm(fields: dict[str, Any], timescale) -> OrbitalElements:
    fields = dict(fields)
    for key, expected in (
        ("CENTER_NAME", "EARTH"), ("REF_FRAME", "TEME"),
        ("TIME_SYSTEM", "UTC"), ("MEAN_ELEMENT_THEORY", "SGP4"),
    ):
        if fields.get(key) not in (None, ""):
            if str(fields[key]).strip().upper() != expected:
                raise ValueError(f"Unsupported {key}: {fields[key]}")

    raw_id = fields["NORAD_CAT_ID"]
    if isinstance(raw_id, bool) or not str(raw_id).isdigit():
        raise ValueError("NORAD_CAT_ID must be a positive integer")
    fields["NORAD_CAT_ID"] = int(raw_id)
    if not 1 <= fields["NORAD_CAT_ID"] <= 999999999:
        raise ValueError("NORAD_CAT_ID must contain one to nine digits")

    for key in _NUMERIC_FIELDS:
        if isinstance(fields[key], bool):
            raise ValueError(f"{key} must be a finite number")
        fields[key] = float(fields[key])
        if not math.isfinite(fields[key]):
            raise ValueError(f"{key} must be a finite number")
    if fields["MEAN_MOTION"] <= 0:
        raise ValueError("MEAN_MOTION must be positive")
    if not 0 <= fields["ECCENTRICITY"] < 1:
        raise ValueError("ECCENTRICITY must be between zero and one")
    if not 0 <= fields["INCLINATION"] <= 180:
        raise ValueError("INCLINATION must be between zero and 180 degrees")

    # Skyfield 1.49's OMM reader requires a fractional, timezone-free UTC epoch.
    epoch = datetime.fromisoformat(str(fields["EPOCH"]))
    if epoch.tzinfo is not None:
        epoch = epoch.astimezone(timezone.utc).replace(tzinfo=None)
    fields["EPOCH"] = epoch.isoformat(timespec="microseconds")
    fields["OBJECT_NAME"] = str(fields.get("OBJECT_NAME") or fields["NORAD_CAT_ID"])
    fields["OBJECT_ID"] = str(fields.get("OBJECT_ID") or "")
    fields["CLASSIFICATION_TYPE"] = str(fields.get("CLASSIFICATION_TYPE") or "U")
    for key in ("EPHEMERIS_TYPE", "ELEMENT_SET_NO", "REV_AT_EPOCH"):
        fields[key] = int(fields.get(key) or 0)
    # SGP4's catalog field is limited to Alpha-5 and does not enter the orbit
    # calculations. Keep larger IDs in the native OMM metadata and cache keys.
    propagation_fields = fields
    if fields["NORAD_CAT_ID"] > 339999:
        propagation_fields = {**fields, "NORAD_CAT_ID": 0}
    satellite = EarthSatellite.from_omm(timescale, propagation_fields)
    return OrbitalElements(satellite=satellite, omm_fields=fields)


def parse_orbital_elements(text: str, timescale) -> list[OrbitalElements]:
    """Detect the response or cache format and skip unusable satellite records."""

    text = text.lstrip("\ufeff \t\r\n")
    records: list[OrbitalElements] = []
    if text.startswith(("[", "{")):
        payload = json.loads(text)
        is_cache = isinstance(payload, dict) and payload.get("format") == _CACHE_FORMAT
        if is_cache:
            entries = payload["satellites"]
        else:
            entries = payload if isinstance(payload, list) else [payload]
        if not isinstance(entries, list):
            raise ValueError("Orbital JSON must contain a list of satellite records")
        for index, entry in enumerate(entries):
            try:
                if not isinstance(entry, dict):
                    raise ValueError("Satellite record must be a JSON object")
                if is_cache and "tle" in entry:
                    name, line1, line2 = entry["tle"]
                    if not line1.startswith("1 ") or not line2.startswith("2 "):
                        raise ValueError("Invalid cached TLE lines")
                    records.append(OrbitalElements(
                        satellite=EarthSatellite(line1, line2, name, timescale),
                        tle_lines=(line1, line2),
                    ))
                else:
                    fields = entry["omm"] if is_cache else entry
                    records.append(_parse_omm(fields, timescale))
            except (KeyError, TypeError, ValueError, OverflowError) as exc:
                LOGGER.warning("Skipping invalid orbital JSON record %s: %s", index, exc)
    else:
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        for index in range(0, len(lines) - 2, 3):
            name, line1, line2 = lines[index:index + 3]
            if not line1.startswith("1 ") or not line2.startswith("2 "):
                continue
            try:
                records.append(OrbitalElements(
                    satellite=EarthSatellite(line1, line2, name, timescale),
                    tle_lines=(line1, line2),
                ))
            except (TypeError, ValueError, OverflowError) as exc:
                LOGGER.warning("Skipping invalid TLE for %s: %s", name, exc)
    return records


def render_orbital_cache(records: list[OrbitalElements]) -> str:
    """Keep TLE-only caches compatible and retain native fields for JSON or mixed data."""

    if all(record.tle_lines is not None for record in records):
        return "".join(
            f"{record.satellite.name}\n{record.tle_lines[0]}\n{record.tle_lines[1]}\n"
            for record in records
        )
    entries = [
        {"tle": [record.satellite.name, *record.tle_lines]}
        if record.tle_lines is not None else {"omm": record.omm_fields}
        for record in records
    ]
    return json.dumps({"format": _CACHE_FORMAT, "satellites": entries}, allow_nan=False) + "\n"
