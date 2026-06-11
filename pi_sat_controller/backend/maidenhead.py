from __future__ import annotations

"""Helpers for converting Maidenhead grid locators into station coordinates."""


def locator_to_lat_lon(locator: str) -> tuple[float, float]:
    """Returns the center latitude/longitude for a Maidenhead locator."""

    normalized = "".join(str(locator or "").strip().split()).upper()
    if len(normalized) not in {4, 6, 8} or len(normalized) % 2:
        raise ValueError("Grid locator must be 4, 6, or 8 characters long.")

    lon = -180.0
    lat = -90.0
    lon_size = 20.0
    lat_size = 10.0

    lon += _letter_index(normalized[0], 18) * lon_size
    lat += _letter_index(normalized[1], 18) * lat_size

    lon_size = 2.0
    lat_size = 1.0
    lon += _digit_index(normalized[2]) * lon_size
    lat += _digit_index(normalized[3]) * lat_size

    if len(normalized) >= 6:
        lon_size = 2.0 / 24.0
        lat_size = 1.0 / 24.0
        lon += _letter_index(normalized[4], 24) * lon_size
        lat += _letter_index(normalized[5], 24) * lat_size

    if len(normalized) >= 8:
        lon_size /= 10.0
        lat_size /= 10.0
        lon += _digit_index(normalized[6]) * lon_size
        lat += _digit_index(normalized[7]) * lat_size

    return (
        round(lat + (lat_size / 2.0), 6),
        round(lon + (lon_size / 2.0), 6),
    )


def _letter_index(value: str, limit: int) -> int:
    index = ord(value) - ord("A")
    if index < 0 or index >= limit:
        raise ValueError("Grid locator contains an invalid letter.")
    return index


def _digit_index(value: str) -> int:
    if value < "0" or value > "9":
        raise ValueError("Grid locator contains an invalid digit.")
    return int(value)
