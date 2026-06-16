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
CAT_DEVICE_SECTION_PREFIX = "cat_device_"


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
class AutomationConfig:
    aos_script: str
    los_script: str


@dataclass(frozen=True)
class CatDeviceConfig:
    device_id: str
    name: str
    connectivity: str
    host: str
    port: int
    serial_port: str
    baud: int | None
    model_id: int | None
    timeout_s: float


@dataclass(frozen=True)
class DeviceConfig:
    enabled: bool
    device_id: str | None
    connectivity: str
    host: str
    port: int
    serial_port: str
    baud: int | None
    model_id: int | None
    target_vfo: str | None
    write_enabled: bool
    timeout_s: float
    shared_local_split_mode: bool = False
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
    automation: AutomationConfig
    cat_devices: dict[str, CatDeviceConfig]
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

    cat_devices = _load_cat_devices(parser)

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
        automation=AutomationConfig(
            aos_script=_get_text(parser, "automation", "aos_script", ""),
            los_script=_get_text(parser, "automation", "los_script", ""),
        ),
        cat_devices=cat_devices,
        rx=_load_device(parser, "rx", cat_devices),
        tx=_load_device(parser, "tx", cat_devices),
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


def _load_device(
    parser: ConfigParser,
    section: str,
    cat_devices: dict[str, CatDeviceConfig] | None = None,
) -> DeviceConfig:
    cat_devices = cat_devices or {}
    device_id = parser.get(section, "device_id", fallback="").strip() or None
    base_device = cat_devices.get(device_id) if device_id else None
    return DeviceConfig(
        enabled=_get_bool(parser, section, "enabled"),
        device_id=device_id,
        connectivity=(
            base_device.connectivity
            if base_device
            else parser.get(section, "connectivity", fallback="network")
        ),
        host=base_device.host if base_device else parser.get(section, "host", fallback=""),
        port=base_device.port if base_device else _get_int(parser, section, "port", fallback=0),
        serial_port=(
            base_device.serial_port
            if base_device
            else parser.get(section, "serial_port", fallback="")
        ),
        baud=base_device.baud if base_device else _get_optional_int(parser, section, "baud"),
        model_id=(
            base_device.model_id
            if base_device
            else _get_optional_int(parser, section, "model_id")
        ),
        target_vfo=parser.get(section, "target_vfo", fallback="").strip() or None,
        shared_local_split_mode=_get_bool(
            parser,
            section,
            "shared_local_split_mode",
            False,
        ),
        cat_debug_logging=_get_bool(parser, section, "cat_debug_logging", False),
        write_enabled=_get_bool(parser, section, "write_enabled"),
        timeout_s=(
            base_device.timeout_s
            if base_device
            else _get_float(parser, section, "timeout_s", fallback=2.0)
        ),
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


def _get_text(
    parser: ConfigParser,
    section: str,
    option: str,
    fallback: str = "",
) -> str:
    if not parser.has_section(section):
        return fallback
    return parser.get(section, option, fallback=fallback)


CAT_DEVICE_FIELDS = [
    "name",
    "connectivity",
    "host",
    "port",
    "serial_port",
    "baud",
    "model_id",
    "timeout_s",
    "capability_comm",
    "capability_ptt",
    "capability_vfo",
    "capability_shared",
    "capability_last_test_utc",
    "capability_notes",
]


SETTINGS_SCHEMA: dict[str, list[str]] = {
    "server": ["host", "port", "gui_resources_caching"],
    "station": ["name", "grid_locator", "latitude_deg", "longitude_deg", "elevation_m"],
    "tle": ["source_url", "cache_dir", "stale_after_hours"],
    "profiles": ["satellites_file"],
    "my_satellites": ["min_pass_elevation_deg", "autotrack_next_pass"],
    "rx": [
        "device_id",
        "target_vfo",
        "cat_debug_logging",
        "write_enabled",
    ],
    "tx": [
        "device_id",
        "target_vfo",
        "shared_local_split_mode",
        "cat_debug_logging",
        "write_enabled",
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
    "automation": ["aos_script", "los_script"],
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
    save_settings(settings, path=path, validate_role_assignments=False)


def load_settings(path: Path | str = DEFAULT_CONFIG_PATH) -> dict[str, dict[str, str]]:
    parser = ConfigParser()
    loaded = parser.read(Path(path))
    if not loaded:
        raise FileNotFoundError(f"Config file not found: {path}")

    cat_devices = _load_cat_devices(parser)
    settings: dict[str, dict[str, str]] = {}
    for section, keys in SETTINGS_SCHEMA.items():
        settings[section] = {}
        section_exists = parser.has_section(section)
        for key in keys:
            if key == "device_id" and section in {"rx", "tx"}:
                raw_value = _resolve_role_device_id(parser, section, cat_devices)
            else:
                raw_value = parser.get(section, key, fallback="") if section_exists else ""
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
    cat_devices: list[dict[str, Any]] | None = None,
    path: Path | str = DEFAULT_CONFIG_PATH,
    validate_role_assignments: bool = True,
) -> None:
    current = load_settings(path)
    current_cat_devices = load_cat_devices(path)
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

    if cat_devices is not None:
        current_cat_devices = _normalize_cat_devices(cat_devices)

    _apply_station_grid_locator(current)
    if validate_role_assignments:
        _validate_role_device_assignments(current, current_cat_devices)

    rendered = _render_settings(current, current_cat_devices)
    Path(path).write_text(rendered, encoding="utf-8")
    load_config(path)


def _validate_role_device_assignments(
    settings: dict[str, dict[str, str]],
    cat_devices: list[dict[str, str]],
) -> None:
    rx_device_id = str(settings.get("rx", {}).get("device_id", "")).strip()
    tx_device_id = str(settings.get("tx", {}).get("device_id", "")).strip()
    if not rx_device_id or not tx_device_id or rx_device_id != tx_device_id:
        return

    device_by_id = {
        str(device.get("device_id", "")).strip(): device
        for device in cat_devices
        if str(device.get("device_id", "")).strip()
    }
    shared_capable = (
        str(device_by_id.get(rx_device_id, {}).get("capability_shared", "")).strip().lower()
        == "true"
    )
    if not shared_capable:
        raise ValueError(
            "The selected CAT device cannot be assigned to both RX and TX. "
            "Save the device and use a shared-capable local radio for dual-role operation."
        )


def _render_settings(
    settings: dict[str, dict[str, str]],
    cat_devices: list[dict[str, str]],
) -> str:
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

    for cat_device in cat_devices:
        lines.append("")
        lines.append(f"[{CAT_DEVICE_SECTION_PREFIX}{cat_device['device_id']}]")
        for key in CAT_DEVICE_FIELDS:
            lines.append(f"{key} = {cat_device.get(key, '')}")

    lines.append("")
    values = section("my_satellites")
    _append_keys(lines, values, SETTINGS_SCHEMA["my_satellites"])
    for key in sorted(key for key in values if key.startswith("satellite_")):
        lines.append(f"{key} = {values[key]}")

    for role in ("rx", "tx"):
        lines.append("")
        values = section(role)
        lines.append(f"enabled = {values['enabled']}")
        lines.append("# Assign a configured CAT device from the My CAT Devices inventory.")
        lines.append(f"device_id = {values['device_id']}")
        lines.append(
            f"# Select which VFO Hamlib should control for {role.upper()}: current, A, or B."
        )
        lines.append(f"target_vfo = {values['target_vfo']}")
        if role == "tx":
            lines.append(
                "# When RX and TX share the same local radio, enable rig split mode for TX updates."
            )
            lines.append(f"shared_local_split_mode = {values['shared_local_split_mode']}")
        lines.append("# Enable verbose CAT/Hamlib command logging for this role.")
        lines.append(f"cat_debug_logging = {values['cat_debug_logging']}")
        lines.append("# Enable frequency writes for this role.")
        lines.append(f"write_enabled = {values['write_enabled']}")

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
    values = section("automation")
    _append_keys(lines, values, SETTINGS_SCHEMA["automation"])

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


def load_cat_devices(
    path: Path | str = DEFAULT_CONFIG_PATH,
) -> list[dict[str, str]]:
    parser = ConfigParser()
    loaded = parser.read(Path(path))
    if not loaded:
        raise FileNotFoundError(f"Config file not found: {path}")
    return [
        {
            "device_id": device.device_id,
            "name": device.name,
            "connectivity": device.connectivity,
            "host": device.host,
            "port": str(device.port),
            "serial_port": device.serial_port,
            "baud": "" if device.baud is None else str(device.baud),
            "model_id": "" if device.model_id is None else str(device.model_id),
            "timeout_s": str(device.timeout_s),
            "capability_comm": _load_cat_device_metadata(
                parser, device.device_id, "capability_comm"
            ),
            "capability_ptt": _load_cat_device_metadata(
                parser, device.device_id, "capability_ptt"
            ),
            "capability_vfo": _load_cat_device_metadata(
                parser, device.device_id, "capability_vfo"
            ),
            "capability_shared": _load_cat_device_metadata(
                parser, device.device_id, "capability_shared"
            ),
            "capability_last_test_utc": _load_cat_device_metadata(
                parser, device.device_id, "capability_last_test_utc"
            ),
            "capability_notes": _load_cat_device_metadata(
                parser, device.device_id, "capability_notes"
            ),
        }
        for device in _load_cat_devices(parser).values()
    ]


def _load_cat_devices(parser: ConfigParser) -> dict[str, CatDeviceConfig]:
    explicit: dict[str, CatDeviceConfig] = {}
    for section in parser.sections():
        if not section.startswith(CAT_DEVICE_SECTION_PREFIX):
            continue
        device_id = section.removeprefix(CAT_DEVICE_SECTION_PREFIX).strip()
        if not device_id:
            continue
        explicit[device_id] = CatDeviceConfig(
            device_id=device_id,
            name=parser.get(section, "name", fallback=device_id),
            connectivity=parser.get(section, "connectivity", fallback="network"),
            host=parser.get(section, "host", fallback=""),
            port=_get_int(parser, section, "port", fallback=0),
            serial_port=parser.get(section, "serial_port", fallback=""),
            baud=_get_optional_int(parser, section, "baud"),
            model_id=_get_optional_int(parser, section, "model_id"),
            timeout_s=_get_float(parser, section, "timeout_s", fallback=2.0),
        )
    if explicit:
        return explicit
    return _build_legacy_cat_devices(parser)


def _load_cat_device_metadata(
    parser: ConfigParser,
    device_id: str,
    option: str,
) -> str:
    section = f"{CAT_DEVICE_SECTION_PREFIX}{device_id}"
    if not parser.has_section(section):
        return ""
    return parser.get(section, option, fallback="")


def _build_legacy_cat_devices(parser: ConfigParser) -> dict[str, CatDeviceConfig]:
    legacy_devices: dict[str, CatDeviceConfig] = {}
    seen_by_signature: dict[tuple[str, str, int, str, int | None, int | None], str] = {}
    for role in ("rx", "tx"):
        if not parser.has_section(role):
            continue
        connectivity = parser.get(role, "connectivity", fallback="").strip()
        if connectivity not in {"network", "local"}:
            continue
        host = parser.get(role, "host", fallback="").strip()
        port = _get_int(parser, role, "port", fallback=0)
        serial_port = parser.get(role, "serial_port", fallback="").strip()
        baud = _get_optional_int(parser, role, "baud")
        model_id = _get_optional_int(parser, role, "model_id")
        if connectivity == "network" and (not host or not port):
            continue
        if connectivity == "local" and (not serial_port or not baud or not model_id):
            continue
        signature = (connectivity, host, port, serial_port, baud, model_id)
        existing_id = seen_by_signature.get(signature)
        if existing_id:
            existing = legacy_devices[existing_id]
            if "Shared" not in existing.name:
                legacy_devices[existing_id] = CatDeviceConfig(
                    device_id=existing.device_id,
                    name="Legacy Shared CAT Device",
                    connectivity=existing.connectivity,
                    host=existing.host,
                    port=existing.port,
                    serial_port=existing.serial_port,
                    baud=existing.baud,
                    model_id=existing.model_id,
                    timeout_s=existing.timeout_s,
                )
            continue
        device_id = f"legacy-{role}"
        if device_id in legacy_devices:
            device_id = f"legacy-{role}-{len(legacy_devices) + 1}"
        seen_by_signature[signature] = device_id
        legacy_devices[device_id] = CatDeviceConfig(
            device_id=device_id,
            name=f"Legacy {role.upper()} Device",
            connectivity=connectivity,
            host=host,
            port=port,
            serial_port=serial_port,
            baud=baud,
            model_id=model_id,
            timeout_s=_get_float(parser, role, "timeout_s", fallback=2.0),
        )
    return legacy_devices


def _resolve_role_device_id(
    parser: ConfigParser,
    role: str,
    cat_devices: dict[str, CatDeviceConfig],
) -> str:
    explicit = parser.get(role, "device_id", fallback="").strip()
    if explicit:
        return explicit
    connectivity = parser.get(role, "connectivity", fallback="").strip()
    host = parser.get(role, "host", fallback="").strip()
    port = _get_int(parser, role, "port", fallback=0)
    serial_port = parser.get(role, "serial_port", fallback="").strip()
    baud = _get_optional_int(parser, role, "baud")
    model_id = _get_optional_int(parser, role, "model_id")
    for device in cat_devices.values():
        if (
            device.connectivity == connectivity
            and device.host == host
            and device.port == port
            and device.serial_port == serial_port
            and device.baud == baud
            and device.model_id == model_id
        ):
            return device.device_id
    return ""


def _normalize_cat_devices(cat_devices: list[dict[str, Any]]) -> list[dict[str, str]]:
    normalized: list[dict[str, str]] = []
    seen_ids: set[str] = set()
    for index, raw_device in enumerate(cat_devices, start=1):
        device_id = str(raw_device.get("device_id", "")).strip().lower()
        if not device_id:
            device_id = f"cat-device-{index}"
        device_id = "".join(
            character if character.isalnum() or character in {"-", "_"} else "-"
            for character in device_id
        ).strip("-_")
        if not device_id:
            device_id = f"cat-device-{index}"
        suffix = 2
        base_id = device_id
        while device_id in seen_ids:
            device_id = f"{base_id}-{suffix}"
            suffix += 1
        seen_ids.add(device_id)
        connectivity = str(raw_device.get("connectivity", "network")).strip() or "network"
        normalized.append(
            {
                "device_id": device_id,
                "name": str(raw_device.get("name", "")).strip() or device_id,
                "connectivity": connectivity,
                "host": str(raw_device.get("host", "")).strip(),
                "port": str(parse_int_value(raw_device.get("port"), 0)),
                "serial_port": str(raw_device.get("serial_port", "")).strip(),
                "baud": stringify_optional_int(raw_device.get("baud")),
                "model_id": stringify_optional_int(raw_device.get("model_id")),
                "timeout_s": str(parse_float_value(raw_device.get("timeout_s"), 2.0)),
                "capability_comm": stringify_capability_value(raw_device.get("capability_comm")),
                "capability_ptt": stringify_capability_value(raw_device.get("capability_ptt")),
                "capability_vfo": stringify_capability_value(raw_device.get("capability_vfo")),
                "capability_shared": stringify_capability_value(raw_device.get("capability_shared")),
                "capability_last_test_utc": str(raw_device.get("capability_last_test_utc", "")).strip(),
                "capability_notes": str(raw_device.get("capability_notes", "")).strip(),
            }
        )
    return normalized


def parse_int_value(value: Any, fallback: int) -> int:
    text = str(value).strip()
    if not text:
        return fallback
    return int(text)


def parse_float_value(value: Any, fallback: float) -> float:
    text = str(value).strip()
    if not text:
        return fallback
    return float(text)


def stringify_optional_int(value: Any) -> str:
    text = str(value).strip()
    if not text:
        return ""
    return str(int(text))


def stringify_capability_value(value: Any) -> str:
    text = str(value).strip().lower()
    if text not in {"true", "false"}:
        return ""
    return text
