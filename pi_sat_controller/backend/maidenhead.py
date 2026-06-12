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


def lat_lon_to_locator(latitude_deg: float, longitude_deg: float, precision: int = 6) -> str:
    """Returns a Maidenhead locator centered on the provided latitude/longitude."""

    if precision not in {4, 6, 8}:
        raise ValueError("Grid locator precision must be 4, 6, or 8 characters.")
    if not (-90.0 <= latitude_deg <= 90.0):
        raise ValueError("Latitude must be between -90 and 90 degrees.")
    if not (-180.0 <= longitude_deg <= 180.0):
        raise ValueError("Longitude must be between -180 and 180 degrees.")

    lon = longitude_deg + 180.0
    lat = latitude_deg + 90.0

    field_lon = int(lon // 20.0)
    field_lat = int(lat // 10.0)
    lon -= field_lon * 20.0
    lat -= field_lat * 10.0

    locator = [
        chr(ord("A") + max(0, min(17, field_lon))),
        chr(ord("A") + max(0, min(17, field_lat))),
    ]

    square_lon = int(lon // 2.0)
    square_lat = int(lat // 1.0)
    lon -= square_lon * 2.0
    lat -= square_lat * 1.0
    locator.extend([str(max(0, min(9, square_lon))), str(max(0, min(9, square_lat)))])

    if precision >= 6:
        subsquare_lon = int(lon / (2.0 / 24.0))
        subsquare_lat = int(lat / (1.0 / 24.0))
        subsquare_lon = max(0, min(23, subsquare_lon))
        subsquare_lat = max(0, min(23, subsquare_lat))
        lon -= subsquare_lon * (2.0 / 24.0)
        lat -= subsquare_lat * (1.0 / 24.0)
        locator.extend(
            [
                chr(ord("A") + subsquare_lon),
                chr(ord("A") + subsquare_lat),
            ]
        )

    if precision >= 8:
        extended_lon = int(lon / ((2.0 / 24.0) / 10.0))
        extended_lat = int(lat / ((1.0 / 24.0) / 10.0))
        locator.extend(
            [
                str(max(0, min(9, extended_lon))),
                str(max(0, min(9, extended_lat))),
            ]
        )

    return "".join(locator)


def _letter_index(value: str, limit: int) -> int:
    index = ord(value) - ord("A")
    if index < 0 or index >= limit:
        raise ValueError("Grid locator contains an invalid letter.")
    return index


def _digit_index(value: str) -> int:
    if value < "0" or value > "9":
        raise ValueError("Grid locator contains an invalid digit.")
    return int(value)
