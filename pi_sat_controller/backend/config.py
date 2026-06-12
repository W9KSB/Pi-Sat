from __future__ import annotations

"""Configuration loading and persistence helpers.

The runtime config is kept in one INI-style file. TLE source URLs are encoded
onto one stored line for ConfigParser compatibility and decoded back into a
newline-delimited list for the UI and TLE manager.
"""

from configparser import ConfigParser
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pi_sat_controller.backend.maidenhead import lat_lon_to_locator, locator_to_lat_lon
from pi_sat_controller.backend.models import MySatellite


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "pi-sat-controller.conf"
MULTI_URL_SEPARATOR = " || "


@dataclass(frozen=True)
class ServerConfig:
    host: str
    port: int
    gui_resources_caching: bool


@dataclass(frozen=True)
class StationConfig:
    name: str
    latitude_deg: float
    longitude_deg: float
    elevation_m: float


@dataclass(frozen=True)
class TleConfig:
    source_url: str
    cache_dir: Path
    stale_after_hours: int


@dataclass(frozen=True)
class ProfilesConfig:
    satellites_file: Path


@dataclass(frozen=True)
class DeviceConfig:
    enabled: bool
    connectivity: str
    host: str
    port: int
    serial_port: str
    baud: int | None
    model_id: int | None
    target_vfo: str | None
    write_enabled: bool
    timeout_s: float
    cat_debug_logging: bool = False
    min_elevation_deg: float | None = None
    home_azimuth_deg: float | None = None
    home_elevation_deg: float | None = None
    return_home_after_pass: bool = False


@dataclass(frozen=True)
class SafetyConfig:
    tx_inhibit_below_horizon: bool
    tx_inhibit_on_cat_loss: bool
    tx_inhibit_without_valid_pass: bool
    frequency_deadband_hz: int
    cat_rate_limit_hz: int
    tracking_update_interval_ms: int
    device_offline_failure_threshold: int


@dataclass(frozen=True)
class AppConfig:
    server: ServerConfig
    station: StationConfig
    tle: TleConfig
    profiles: ProfilesConfig
    rx: DeviceConfig
    tx: DeviceConfig
    rotator: DeviceConfig
    safety: SafetyConfig


def _decode_source_url(value: str) -> str:
    return "\n".join(
        part.strip()
        for part in value.split(MULTI_URL_SEPARATOR)
        if part.strip()
    )


def _encode_source_url(value: str) -> str:
    return MULTI_URL_SEPARATOR.join(
        line.strip()
        for line in value.splitlines()
        if line.strip()
    )


def _resolve_path(value: str) -> Path:
    path = Path(value)
    if path.is_absolute():
        return path
    return PROJECT_ROOT / path


def load_config(path: Path | str = DEFAULT_CONFIG_PATH) -> AppConfig:
    config_path = Path(path)
    parser = ConfigParser()
    loaded = parser.read(config_path)
    if not loaded:
        raise FileNotFoundError(f"Config file not found: {config_path}")

    return AppConfig(
        server=ServerConfig(
            host=parser.get("server", "host"),
            port=_get_int(parser, "server", "port"),
            gui_resources_caching=_get_bool(
                parser,
                "server",
                "gui_resources_caching",
                False,
            ),
        ),
        station=StationConfig(
            name=parser.get("station", "name"),
            latitude_deg=_get_float(parser, "station", "latitude_deg"),
            longitude_deg=_get_float(parser, "station", "longitude_deg"),
            elevation_m=_get_float(parser, "station", "elevation_m"),
        ),
        tle=TleConfig(
            source_url=_decode_source_url(parser.get("tle", "source_url")),
            cache_dir=_resolve_path(parser.get("tle", "cache_dir")),
            stale_after_hours=_get_int(parser, "tle", "stale_after_hours"),
        ),
        profiles=ProfilesConfig(
            satellites_file=_resolve_path(parser.get("profiles", "satellites_file")),
        ),
        rx=_load_device(parser, "rx"),
        tx=_load_device(parser, "tx"),
        rotator=_load_device(parser, "rotator"),
        safety=SafetyConfig(
            tx_inhibit_below_horizon=parser.getboolean(
                "safety", "tx_inhibit_below_horizon"
            ),
            tx_inhibit_on_cat_loss=parser.getboolean(
                "safety", "tx_inhibit_on_cat_loss"
            ),
            tx_inhibit_without_valid_pass=parser.getboolean(
                "safety", "tx_inhibit_without_valid_pass"
            ),
            frequency_deadband_hz=_get_int(parser, "safety", "frequency_deadband_hz"),
            cat_rate_limit_hz=_get_int(parser, "safety", "cat_rate_limit_hz"),
            tracking_update_interval_ms=_get_int(
                parser,
                "safety",
                "tracking_update_interval_ms",
                fallback=1000,
            ),
            device_offline_failure_threshold=_get_int(
                parser,
                "safety",
                "device_offline_failure_threshold",
                fallback=3,
            ),
        ),
    )


def _load_device(parser: ConfigParser, section: str) -> DeviceConfig:
    return DeviceConfig(
        enabled=_get_bool(parser, section, "enabled"),
        connectivity=parser.get(section, "connectivity"),
        host=parser.get(section, "host"),
        port=_get_int(parser, section, "port"),
        serial_port=parser.get(section, "serial_port", fallback=""),
        baud=_get_optional_int(parser, section, "baud"),
        model_id=_get_optional_int(parser, section, "model_id"),
        target_vfo=parser.get(section, "target_vfo", fallback="").strip() or None,
        cat_debug_logging=_get_bool(parser, section, "cat_debug_logging", False),
        write_enabled=_get_bool(parser, section, "write_enabled"),
        timeout_s=_get_float(parser, section, "timeout_s"),
        min_elevation_deg=_get_optional_float(parser, section, "min_elevation_deg"),
        home_azimuth_deg=_get_optional_float(parser, section, "home_azimuth_deg"),
        home_elevation_deg=_get_optional_float(parser, section, "home_elevation_deg"),
        return_home_after_pass=_get_bool(
            parser,
            section,
            "return_home_after_pass",
            False,
        ),
    )


def _get_optional_int(parser: ConfigParser, section: str, option: str) -> int | None:
    value = parser.get(section, option, fallback="").strip()
    if not value:
        return None
    return int(value)


def _get_int(
    parser: ConfigParser,
    section: str,
    option: str,
    fallback: int | None = None,
) -> int:
    value = parser.get(section, option, fallback="").strip()
    if not value:
        if fallback is not None:
            return fallback
        raise ValueError(f"Missing integer value for [{section}] {option}")
    return int(value)


def _get_optional_float(
    parser: ConfigParser, section: str, option: str
) -> float | None:
    value = parser.get(section, option, fallback="").strip()
    if not value:
        return None
    return float(value)


def _get_float(
    parser: ConfigParser,
    section: str,
    option: str,
    fallback: float | None = None,
) -> float:
    value = parser.get(section, option, fallback="").strip()
    if not value:
        if fallback is not None:
            return fallback
        raise ValueError(f"Missing float value for [{section}] {option}")
    return float(value)


def _get_bool(
    parser: ConfigParser,
    section: str,
    option: str,
    fallback: bool = False,
) -> bool:
    value = parser.get(section, option, fallback="").strip().lower()
    if not value:
        return fallback
    return value in {"1", "yes", "true", "on"}


SETTINGS_SCHEMA: dict[str, list[str]] = {
    "server": ["host", "port", "gui_resources_caching"],
    "station": ["name", "grid_locator", "latitude_deg", "longitude_deg", "elevation_m"],
    "tle": ["source_url", "cache_dir", "stale_after_hours"],
    "profiles": ["satellites_file"],
    "my_satellites": ["min_pass_elevation_deg", "autotrack_next_pass"],
    "rx": [
        "connectivity",
        "host",
        "port",
        "serial_port",
        "baud",
        "model_id",
        "target_vfo",
        "cat_debug_logging",
        "write_enabled",
        "timeout_s",
    ],
    "tx": [
        "connectivity",
        "host",
        "port",
        "serial_port",
        "baud",
        "model_id",
        "target_vfo",
        "cat_debug_logging",
        "write_enabled",
        "timeout_s",
    ],
    "rotator": [
        "connectivity",
        "host",
        "port",
        "serial_port",
        "baud",
        "model_id",
        "cat_debug_logging",
        "write_enabled",
        "timeout_s",
        "min_elevation_deg",
        "home_azimuth_deg",
        "home_elevation_deg",
        "return_home_after_pass",
    ],
    "safety": [
        "tx_inhibit_below_horizon",
        "tx_inhibit_on_cat_loss",
        "tx_inhibit_without_valid_pass",
        "frequency_deadband_hz",
        "cat_rate_limit_hz",
        "tracking_update_interval_ms",
        "device_offline_failure_threshold",
    ],
}


def load_my_satellites(
    path: Path | str = DEFAULT_CONFIG_PATH,
) -> tuple[list[MySatellite], float, bool]:
    parser = ConfigParser()
    loaded = parser.read(Path(path))
    if not loaded:
        raise FileNotFoundError(f"Config file not found: {path}")
    if not parser.has_section("my_satellites"):
        return [], 10.0, False

    satellites: list[MySatellite] = []
    for key, value in parser.items("my_satellites"):
        if not key.startswith("satellite_"):
            continue
        try:
            norad_id = int(key.removeprefix("satellite_"))
        except ValueError:
            continue
        satellites.append(MySatellite(norad_id=norad_id, name=value.strip()))

    return (
        sorted(satellites, key=lambda satellite: satellite.name.lower()),
        parser.getfloat("my_satellites", "min_pass_elevation_deg", fallback=10.0),
        parser.getboolean("my_satellites", "autotrack_next_pass", fallback=True),
    )


def save_my_satellites(
    satellites: list[MySatellite],
    min_pass_elevation_deg: float,
    autotrack_next_pass: bool,
    path: Path | str = DEFAULT_CONFIG_PATH,
) -> None:
    settings = load_settings(path)
    settings["my_satellites"] = {
        key: value
        for key, value in settings["my_satellites"].items()
        if not key.startswith("satellite_")
    }
    settings["my_satellites"]["min_pass_elevation_deg"] = str(min_pass_elevation_deg)
    settings["my_satellites"]["autotrack_next_pass"] = (
        "true" if autotrack_next_pass else "false"
    )
    for satellite in satellites:
        settings["my_satellites"][f"satellite_{satellite.norad_id}"] = satellite.name
    save_settings(settings, path)


def load_settings(path: Path | str = DEFAULT_CONFIG_PATH) -> dict[str, dict[str, str]]:
    parser = ConfigParser()
    loaded = parser.read(Path(path))
    if not loaded:
        raise FileNotFoundError(f"Config file not found: {path}")

    settings: dict[str, dict[str, str]] = {}
    for section, keys in SETTINGS_SCHEMA.items():
        settings[section] = {}
        for key in keys:
            raw_value = parser.get(section, key, fallback="")
            if section == "tle" and key == "source_url":
                settings[section][key] = _decode_source_url(raw_value)
            elif section == "station" and key == "grid_locator":
                settings[section][key] = _load_station_grid_locator(parser)
            else:
                settings[section][key] = raw_value
    for section in ("rx", "tx", "rotator"):
        settings[section]["enabled"] = parser.get(section, "enabled", fallback="false")
    if parser.has_section("my_satellites"):
        for key, value in parser.items("my_satellites"):
            if key.startswith("satellite_"):
                settings["my_satellites"][key] = value
    return settings


def save_settings(
    settings: dict[str, dict[str, Any]],
    path: Path | str = DEFAULT_CONFIG_PATH,
) -> None:
    current = load_settings(path)
    for section, values in settings.items():
        if section not in SETTINGS_SCHEMA:
            continue
        if section == "my_satellites" and any(
            key.startswith("satellite_") for key in values
        ):
            current[section] = {
                key: value
                for key, value in current[section].items()
                if not key.startswith("satellite_")
            }
        for key, value in values.items():
            if key == "enabled" and section in {"rx", "tx", "rotator"}:
                current[section][key] = "true" if str(value).lower() == "true" else "false"
            elif key in SETTINGS_SCHEMA[section] or (
                section == "my_satellites" and key.startswith("satellite_")
            ):
                if section == "tle" and key == "source_url":
                    current[section][key] = "" if value is None else _encode_source_url(str(value))
                else:
                    current[section][key] = "" if value is None else str(value)

    _apply_station_grid_locator(current)

    rendered = _render_settings(current)
    Path(path).write_text(rendered, encoding="utf-8")
    load_config(path)


def _render_settings(settings: dict[str, dict[str, str]]) -> str:
    lines: list[str] = []

    def section(name: str) -> dict[str, str]:
        lines.append(f"[{name}]")
        return settings[name]

    values = section("server")
    lines.append(f"host = {values['host']}")
    lines.append(f"port = {values['port']}")
    lines.append("# Enable browser caching for GUI static resources.")
    lines.append(f"gui_resources_caching = {values['gui_resources_caching']}")

    lines.append("")
    values = section("station")
    _append_keys(lines, values, SETTINGS_SCHEMA["station"])

    lines.append("")
    values = section("tle")
    _append_keys(lines, values, SETTINGS_SCHEMA["tle"])

    lines.append("")
    values = section("profiles")
    _append_keys(lines, values, SETTINGS_SCHEMA["profiles"])

    lines.append("")
    values = section("my_satellites")
    _append_keys(lines, values, SETTINGS_SCHEMA["my_satellites"])
    for key in sorted(key for key in values if key.startswith("satellite_")):
        lines.append(f"{key} = {values[key]}")

    for role in ("rx", "tx"):
        lines.append("")
        values = section(role)
        lines.append(f"enabled = {values['enabled']}")
        lines.append(
            "# Set below to local for USB/serial connected devices and network for rigctld/Hamlib devices."
        )
        lines.append(f"connectivity = {values['connectivity']}")
        lines.append("# If connectivity is set to network, these values below are used.")
        lines.append(f"host = {values['host']}")
        lines.append(f"port = {values['port']}")
        lines.append("# If connectivity is set to local, these values below are used.")
        lines.append(f"serial_port = {values['serial_port']}")
        lines.append(f"baud = {values['baud']}")
        lines.append(f"model_id = {values['model_id']}")
        lines.append(
            f"# Select which VFO Hamlib should control for {role.upper()}: current, A, or B."
        )
        lines.append(f"target_vfo = {values['target_vfo']}")
        lines.append("# Enable verbose CAT/Hamlib command logging for this role.")
        lines.append(f"cat_debug_logging = {values['cat_debug_logging']}")
        lines.append("# Enable frequency writes for this role.")
        lines.append(f"write_enabled = {values['write_enabled']}")
        lines.append(f"timeout_s = {values['timeout_s']}")

    lines.append("")
    values = section("rotator")
    lines.append(f"enabled = {values['enabled']}")
    lines.append(
        "# Set below to local for USB/serial connected devices and network for rotctld/Hamlib devices."
    )
    lines.append(f"connectivity = {values['connectivity']}")
    lines.append("# If connectivity is set to network, these values below are used.")
    lines.append(f"host = {values['host']}")
    lines.append(f"port = {values['port']}")
    lines.append("# If connectivity is set to local, these values below are used.")
    lines.append(f"serial_port = {values['serial_port']}")
    lines.append(f"baud = {values['baud']}")
    lines.append(f"model_id = {values['model_id']}")
    lines.append("# Enable verbose rotator/Hamlib command logging for this role.")
    lines.append(f"cat_debug_logging = {values['cat_debug_logging']}")
    lines.append("# Enable rotator position writes. Keep false until hardware is ready.")
    lines.append(f"write_enabled = {values['write_enabled']}")
    lines.append(f"timeout_s = {values['timeout_s']}")
    lines.append(f"min_elevation_deg = {values['min_elevation_deg']}")
    lines.append(f"home_azimuth_deg = {values['home_azimuth_deg']}")
    lines.append(f"home_elevation_deg = {values['home_elevation_deg']}")
    lines.append(f"return_home_after_pass = {values['return_home_after_pass']}")

    lines.append("")
    values = section("safety")
    _append_keys(lines, values, SETTINGS_SCHEMA["safety"])
    lines.append("")
    return "\n".join(lines)


def _append_keys(lines: list[str], values: dict[str, str], keys: list[str]) -> None:
    for key in keys:
        value = values[key]
        if key == "source_url":
            value = _encode_source_url(value)
        lines.append(f"{key} = {value}")


def _load_station_grid_locator(parser: ConfigParser) -> str:
    explicit = parser.get("station", "grid_locator", fallback="").strip().upper()
    if explicit:
        return explicit
    latitude_deg = _get_float(parser, "station", "latitude_deg")
    longitude_deg = _get_float(parser, "station", "longitude_deg")
    return lat_lon_to_locator(latitude_deg, longitude_deg, precision=6)


def _apply_station_grid_locator(settings: dict[str, dict[str, str]]) -> None:
    station = settings.get("station", {})
    locator = str(station.get("grid_locator", "")).strip().upper()
    if not locator:
        latitude_deg = float(station["latitude_deg"])
        longitude_deg = float(station["longitude_deg"])
        station["grid_locator"] = lat_lon_to_locator(latitude_deg, longitude_deg, precision=6)
        return
    if len(locator) != 6:
        raise ValueError("Station grid locator must be exactly 6 characters.")
    latitude_deg, longitude_deg = locator_to_lat_lon(locator)
    station["grid_locator"] = locator
    station["latitude_deg"] = str(latitude_deg)
    station["longitude_deg"] = str(longitude_deg)
