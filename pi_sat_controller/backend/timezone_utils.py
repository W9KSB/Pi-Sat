from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

try:
    from timezonefinderL import TimezoneFinder
except ImportError:  # pragma: no cover - fallback for older envs
    from timezonefinder import TimezoneFinder


_TIMEZONE_FINDER = TimezoneFinder()
_DEFAULT_TIMEZONE = "UTC"


def qth_timezone_name(latitude_deg: float, longitude_deg: float) -> str:
    timezone_name = _TIMEZONE_FINDER.timezone_at(
        lat=latitude_deg,
        lng=longitude_deg,
    )
    return timezone_name or _DEFAULT_TIMEZONE


def to_local_iso(value: datetime, timezone_name: str) -> str:
    utc_value = _as_utc(value)
    return utc_value.astimezone(ZoneInfo(timezone_name)).isoformat()


def to_local_label(value: datetime, timezone_name: str) -> str:
    local_value = _as_utc(value).astimezone(ZoneInfo(timezone_name))
    return local_value.strftime("%m/%d/%Y, %I:%M:%S %p")


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)
