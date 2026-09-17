from __future__ import annotations

"""Configuration loading and persistence helpers.

The runtime config is kept in one INI-style file. TLE source URLs are encoded
onto one stored line for ConfigParser compatibility and decoded back into a
newline-delimited list for the UI and TLE manager.
"""

from configparser import ConfigParser
from contextlib import contextmanager
from dataclasses import dataclass
import logging
import os
from pathlib import Path
from tempfile import NamedTemporaryFile
from threading import RLock
from typing import Any

from pi_sat_controller.backend.audio_gain import RX_GAIN_DEFAULT_DB, clamp_gain_db
from pi_sat_controller.backend.maidenhead import lat_lon_to_locator, locator_to_lat_lon
from pi_sat_controller.backend.models import MySatellite

LOGGER = logging.getLogger(__name__)


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "pi-sat-controller.conf"
MULTI_URL_SEPARATOR = " || "
CAT_DEVICE_SECTION_PREFIX = "cat_device_"
NATIVE_ICOM_DEVICE_ID = "native-ic9700"
_CONFIG_WRITE_LOCK = RLock()


@contextmanager
def config_transaction():
    """Serializes a complete configuration read-modify-write transaction."""

    with _CONFIG_WRITE_LOCK:
        yield


@dataclass(frozen=True)
class ServerConfig:
    host: str
    port: int
    gui_resources_caching: bool
    https_enabled: bool = True
    https_port: int = 443
    tls_certfile: Path = PROJECT_ROOT / "data/tls/server.crt"
    tls_keyfile: Path = PROJECT_ROOT / "data/tls/server.key"
    tls_names: str = ""

    @property
    def listen_port(self) -> int:
        return self.https_port if self.https_enabled else self.port


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
    state_updates: str = "automatic"


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
    # Device writes are controlled solely by the runtime enabled toggle.
    write_enabled: bool
    timeout_s: float
    state_updates: str = "automatic"
    shared_local_split_mode: bool = False
    cat_debug_logging: bool = False
    min_elevation_deg: float | None = None
    home_azimuth_deg: float | None = None
    home_elevation_deg: float | None = None
    return_home_after_pass: bool = False


@dataclass(frozen=True)
class SafetyConfig:
    frequency_deadband_hz: int
    cat_rate_limit_hz: int
    tracking_update_interval_ms: int
    device_offline_failure_threshold: int
    manual_offset_readback_active_pass_only: bool


@dataclass(frozen=True)
class IcomConfig:
    enabled: bool
    connectivity: str
    host: str
    username: str
    password: str
    control_port: int
    serial_port: int
    audio_port: int
    civ_address: int
    controller_address: int
    sample_rate: int
    rx_codec: str
    tx_codec: str
    full_duplex: bool
    scope_enabled: bool
    debug_logging: bool


@dataclass(frozen=True)
class AprsConfig:
    mycall: str
    channel: str
    destination: str
    path: str
    symbol: str
    comment: str
    tx_delay_ms: int
    tx_tail_ms: int
    amplitude: int
    min_interval_s: int
    rx_gain_db: float


@dataclass(frozen=True)
class SstvConfig:
    rx_gain_db: float


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
    icom: IcomConfig
    aprs: AprsConfig
    sstv: SstvConfig


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
            https_enabled=parser.getboolean("server", "https_enabled", fallback=True),
            https_port=_get_int(parser, "server", "https_port", 443),
            tls_certfile=_resolve_path(_get_text(parser, "server", "tls_certfile", "data/tls/server.crt")),
            tls_keyfile=_resolve_path(_get_text(parser, "server", "tls_keyfile", "data/tls/server.key")),
            tls_names=_get_text(parser, "server", "tls_names", ""),
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
            manual_offset_readback_active_pass_only=_get_bool(
                parser,
                "safety",
                "manual_offset_readback_active_pass_only",
                fallback=False,
            ),
        ),
        icom=IcomConfig(
            enabled=_get_bool(parser, "icom", "enabled", False),
            connectivity=_get_icom_connectivity(parser),
            host=parser.get("icom", "host", fallback=""),
            username=parser.get("icom", "username", fallback=""),
            password=parser.get("icom", "password", fallback=""),
            control_port=_get_int(parser, "icom", "control_port", 50001),
            serial_port=_get_int(parser, "icom", "serial_port", 50002),
            audio_port=_get_int(parser, "icom", "audio_port", 50003),
            civ_address=_get_address(parser, "icom", "civ_address", 0xA2),
            controller_address=_get_address(parser, "icom", "controller_address", 0xE0),
            sample_rate=_get_int(parser, "icom", "sample_rate", 16000),
            rx_codec=parser.get("icom", "rx_codec", fallback="lpcm16_stereo"),
            tx_codec=parser.get("icom", "tx_codec", fallback="lpcm16_mono"),
            full_duplex=_get_bool(parser, "icom", "full_duplex", True),
            scope_enabled=_get_bool(parser, "icom", "scope_enabled", True),
            debug_logging=_get_bool(parser, "icom", "debug_logging", False),
        ),
        aprs=AprsConfig(
            mycall=_get_text(parser, "aprs", "mycall", ""),
            channel=_get_aprs_channel(parser),
            destination=_get_text(parser, "aprs", "destination", "APDW16"),
            path=_get_text(parser, "aprs", "path", "WIDE1-1,WIDE2-1"),
            symbol=_get_text(parser, "aprs", "symbol", "/>"),
            comment=_get_text(parser, "aprs", "comment", "Pi-Sat"),
            tx_delay_ms=_get_int(parser, "aprs", "tx_delay_ms", 300),
            tx_tail_ms=_get_int(parser, "aprs", "tx_tail_ms", 200),
            amplitude=_get_int(parser, "aprs", "amplitude", 100),
            min_interval_s=_get_int(parser, "aprs", "min_interval_s", 30),
            rx_gain_db=_get_rx_gain_db(parser, "aprs"),
        ),
        sstv=SstvConfig(rx_gain_db=_get_rx_gain_db(parser, "sstv")),
    )


def _load_device(
    parser: ConfigParser,
    section: str,
    cat_devices: dict[str, CatDeviceConfig] | None = None,
) -> DeviceConfig:
    cat_devices = cat_devices or {}
    device_id = parser.get(section, "device_id", fallback="").strip() or None
    if (
        section in {"rx", "tx"}
        and device_id is None
        and _get_bool(parser, "icom", "enabled", False)
    ):
        # A blank RX/TX device selects native Icom when it is enabled; explicit
        # generic selections remain unchanged.
        device_id = NATIVE_ICOM_DEVICE_ID
    is_native_icom = device_id == NATIVE_ICOM_DEVICE_ID
    base_device = cat_devices.get(device_id) if device_id else None
    return DeviceConfig(
        enabled=_get_bool(parser, section, "enabled"),
        device_id=device_id,
        connectivity=("native" if is_native_icom else
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
        target_vfo=(
            ("SUB" if section == "rx" else "MAIN")
            if is_native_icom
            else parser.get(section, "target_vfo", fallback="").strip() or None
        ),
        shared_local_split_mode=_get_bool(
            parser,
            section,
            "shared_local_split_mode",
            False,
        ),
        cat_debug_logging=_get_bool(parser, section, "cat_debug_logging", False),
        write_enabled=True,
        timeout_s=(
            base_device.timeout_s
            if base_device
            else _get_float(parser, section, "timeout_s", fallback=2.0)
        ),
        state_updates=(
            base_device.state_updates
            if base_device
            else parser.get(section, "state_updates", fallback="automatic")
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


def _get_address(
    parser: ConfigParser,
    section: str,
    option: str,
    fallback: int,
) -> int:
    value = parser.get(section, option, fallback="").strip()
    if not value:
        return fallback
    try:
        return int(value, 0)
    except ValueError as exc:
        raise ValueError(f"Invalid hexadecimal/decimal address for [{section}] {option}") from exc


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


def _get_icom_connectivity(parser: ConfigParser) -> str:
    """Return the supported LAN transport, tolerating unsupported stored values."""
    connectivity = parser.get("icom", "connectivity", fallback="network").strip().lower()
    if connectivity != "network":
        LOGGER.warning(
            "[icom] connectivity=%r is unsupported; using the network transport",
            connectivity,
        )
    return "network"


def _get_aprs_channel(parser: ConfigParser) -> str:
    """Which receiver the APRS decoder listens to.

    "main" and "right" name the left and right halves of the stereo RX stream;
    "both" averages them. Which physical side is left is not fixed by the
    Icom protocol, so this is an operator choice rather than an assumption.
    """
    value = parser.get("aprs", "channel", fallback="").strip().lower()
    if not value:
        return "main"
    if value not in {"main", "right", "both"}:
        raise ValueError("[aprs] channel must be main, right, or both")
    return value


APRS_RX_GAIN_DEFAULT_DB = RX_GAIN_DEFAULT_DB


def _get_rx_gain_db(parser: ConfigParser, section: str) -> float:
    """Level trim applied only to one module's copy of the receive audio.

    The radio's receive level is set for listening rather than for a decoder,
    and decoders want the audio near half scale. This trim lives purely in that
    module's own stream: it never touches the radio, the browser playback level,
    or any other audio consumer.
    """
    try:
        value = _get_float(parser, section, "rx_gain_db", RX_GAIN_DEFAULT_DB)
    except ValueError as exc:
        raise ValueError(f"[{section}] rx_gain_db must be a number of decibels") from exc
    try:
        return clamp_gain_db(value)
    except ValueError as exc:
        raise ValueError(f"[{section}] rx_gain_db: {exc}") from exc


CAT_DEVICE_FIELDS = [
    "name",
    "connectivity",
    "host",
    "port",
    "serial_port",
    "baud",
    "model_id",
    "timeout_s",
    "state_updates",
    "capability_comm",
    "capability_ptt",
    "capability_vfo",
    "capability_shared",
    "capability_targets",
    "capability_last_test_utc",
    "capability_notes",
    "capability_async",
    "capability_async_version",
    "capability_async_properties",
    "capability_async_notes",
]


SETTINGS_SCHEMA: dict[str, list[str]] = {
    "server": ["host", "port", "gui_resources_caching", "https_enabled", "https_port",
               "tls_certfile", "tls_keyfile", "tls_names"],
    "station": ["name", "grid_locator", "latitude_deg", "longitude_deg", "elevation_m"],
    "tle": ["source_url", "cache_dir", "stale_after_hours"],
    "profiles": ["satellites_file"],
    "my_satellites": [
        "min_pass_elevation_deg",
        "autotrack_next_pass",
        "autotrack_norad_ids",
    ],
    "rx": [
        "device_id",
        "target_vfo",
        "cat_debug_logging",
    ],
    "tx": [
        "device_id",
        "target_vfo",
        "shared_local_split_mode",
        "cat_debug_logging",
    ],
    "rotator": [
        "connectivity",
        "host",
        "port",
        "serial_port",
        "baud",
        "model_id",
        "cat_debug_logging",
        "timeout_s",
        "min_elevation_deg",
        "home_azimuth_deg",
        "home_elevation_deg",
        "return_home_after_pass",
    ],
    "automation": ["aos_script", "los_script"],
    "safety": [
        "frequency_deadband_hz",
        "cat_rate_limit_hz",
        "tracking_update_interval_ms",
        "device_offline_failure_threshold",
        "manual_offset_readback_active_pass_only",
    ],
    "icom": [
        "enabled",
        "connectivity",
        "host",
        "username",
        "password",
        "control_port",
        "serial_port",
        "audio_port",
        "civ_address",
        "controller_address",
        "sample_rate",
        "rx_codec",
        "tx_codec",
        "full_duplex",
        "scope_enabled",
        "debug_logging",
    ],
    "aprs": [
        "mycall",
        "channel",
        "destination",
        "path",
        "symbol",
        "comment",
        "tx_delay_ms",
        "tx_tail_ms",
        "amplitude",
        "min_interval_s",
        "rx_gain_db",
    ],
    "sstv": [
        "rx_gain_db",
    ],
}


def load_my_satellites(
    path: Path | str = DEFAULT_CONFIG_PATH,
) -> tuple[list[MySatellite], float, bool, set[int]]:
    parser = ConfigParser()
    loaded = parser.read(Path(path))
    if not loaded:
        raise FileNotFoundError(f"Config file not found: {path}")
    if not parser.has_section("my_satellites"):
        return [], 10.0, False, set()

    satellites: list[MySatellite] = []
    for key, value in parser.items("my_satellites"):
        if not key.startswith("satellite_"):
            continue
        try:
            norad_id = int(key.removeprefix("satellite_"))
        except ValueError:
            continue
        satellites.append(MySatellite(norad_id=norad_id, name=value.strip()))

    satellites = sorted(satellites, key=lambda satellite: satellite.name.lower())
    configured_norads = {satellite.norad_id for satellite in satellites}
    raw_autotrack_norads = parser.get(
        "my_satellites",
        "autotrack_norad_ids",
        fallback=None,
    )
    if raw_autotrack_norads is None:
        autotrack_norads = configured_norads
    else:
        autotrack_norads = set()
        for value in raw_autotrack_norads.split(","):
            try:
                norad_id = int(value.strip())
            except ValueError:
                continue
            if norad_id in configured_norads:
                autotrack_norads.add(norad_id)

    return (
        satellites,
        parser.getfloat("my_satellites", "min_pass_elevation_deg", fallback=10.0),
        parser.getboolean("my_satellites", "autotrack_next_pass", fallback=True),
        autotrack_norads,
    )


def save_my_satellites(
    satellites: list[MySatellite],
    min_pass_elevation_deg: float,
    autotrack_next_pass: bool,
    autotrack_norad_ids: set[int],
    path: Path | str = DEFAULT_CONFIG_PATH,
) -> None:
    with _CONFIG_WRITE_LOCK:
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
        configured_norads = {satellite.norad_id for satellite in satellites}
        settings["my_satellites"]["autotrack_norad_ids"] = ",".join(
            str(norad_id)
            for norad_id in sorted(autotrack_norad_ids & configured_norads)
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
                if not raw_value and _get_bool(parser, "icom", "enabled", False):
                    raw_value = NATIVE_ICOM_DEVICE_ID
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
    for key, default in {
        "https_enabled": "true", "https_port": "443",
        "tls_certfile": "data/tls/server.crt", "tls_keyfile": "data/tls/server.key",
    }.items():
        if not settings["server"].get(key, "").strip():
            settings["server"][key] = default
    if "icom" in settings:
        icom_defaults = {
            "enabled": "false",
            "control_port": "50001",
            "serial_port": "50002",
            "audio_port": "50003",
            "civ_address": "0xA2",
            "controller_address": "0xE0",
            "sample_rate": "16000",
            "rx_codec": "lpcm16_stereo",
            "tx_codec": "lpcm16_mono",
            "full_duplex": "true",
            "scope_enabled": "true",
            "debug_logging": "false",
        }
        for key, default in icom_defaults.items():
            if not str(settings["icom"].get(key, "")).strip():
                settings["icom"][key] = default
        # Native Icom uses LAN; normalize stored values so browser state and the
        # next save use the supported setting.
        settings["icom"]["connectivity"] = "network"
    if "aprs" in settings:
        # "mycall" intentionally has no default: an empty callsign keeps APRS
        # transmit disabled rather than falling back to Dire Wolf's N0CALL.
        aprs_defaults = {
            "channel": "main",
            "destination": "APDW16",
            "path": "WIDE1-1,WIDE2-1",
            "symbol": "/>",
            "comment": "Pi-Sat",
            "tx_delay_ms": "300",
            "tx_tail_ms": "200",
            "amplitude": "100",
            "min_interval_s": "30",
            "rx_gain_db": f"{APRS_RX_GAIN_DEFAULT_DB:g}",
        }
        for key, default in aprs_defaults.items():
            if not str(settings["aprs"].get(key, "")).strip():
                settings["aprs"][key] = default
    if "sstv" in settings:
        if not str(settings["sstv"].get("rx_gain_db", "")).strip():
            settings["sstv"]["rx_gain_db"] = f"{RX_GAIN_DEFAULT_DB:g}"
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
    with _CONFIG_WRITE_LOCK:
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
                    current[section][key] = (
                        "true" if str(value).lower() == "true" else "false"
                    )
                elif key in SETTINGS_SCHEMA[section] or (
                    section == "my_satellites" and key.startswith("satellite_")
                ):
                    if section == "tle" and key == "source_url":
                        current[section][key] = (
                            "" if value is None else _encode_source_url(str(value))
                        )
                    else:
                        current[section][key] = "" if value is None else str(value)

        if cat_devices is not None:
            current_cat_devices, id_mapping = _normalize_cat_devices(cat_devices)
            for role in ("rx", "tx"):
                selected_id = current.get(role, {}).get("device_id", "").strip()
                if selected_id in id_mapping:
                    current[role]["device_id"] = id_mapping[selected_id]

        _apply_station_grid_locator(current)
        if validate_role_assignments:
            _validate_role_device_assignments(current, current_cat_devices)

        rendered = _render_settings(current, current_cat_devices)
        _write_validated_config(Path(path), rendered)


def _write_validated_config(path: Path, rendered: str) -> None:
    path = path.resolve()
    temporary_path: Path | None = None
    try:
        with NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as temporary:
            temporary.write(rendered)
            temporary.flush()
            os.fsync(temporary.fileno())
            temporary_path = Path(temporary.name)
        load_config(temporary_path)
        os.replace(temporary_path, path)
        temporary_path = None
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def _validate_role_device_assignments(
    settings: dict[str, dict[str, str]],
    cat_devices: list[dict[str, str]],
) -> None:
    rx_device_id = str(settings.get("rx", {}).get("device_id", "")).strip()
    tx_device_id = str(settings.get("tx", {}).get("device_id", "")).strip()
    rx_native = rx_device_id == NATIVE_ICOM_DEVICE_ID
    tx_native = tx_device_id == NATIVE_ICOM_DEVICE_ID
    rx_enabled = str(settings.get("rx", {}).get("enabled", "false")).lower() == "true"
    tx_enabled = str(settings.get("tx", {}).get("enabled", "false")).lower() == "true"
    if rx_native or tx_native:
        enabled_generic_beside_native = (
            (rx_native and not tx_native and tx_enabled)
            or (tx_native and not rx_native and rx_enabled)
        )
        if enabled_generic_beside_native:
            raise ValueError(
                "Native IC-9700 cannot run beside an enabled generic radio role. "
                "Assign Native IC-9700 to both enabled roles or disable the other role."
            )
        return
    if not rx_device_id or not tx_device_id:
        return

    device_by_id = {
        str(device.get("device_id", "")).strip(): device
        for device in cat_devices
        if str(device.get("device_id", "")).strip()
    }
    rx_device = device_by_id.get(rx_device_id, {})
    tx_device = device_by_id.get(tx_device_id, {})
    same_local_endpoint = (
        str(rx_device.get("connectivity", "")).strip().lower() == "local"
        and str(tx_device.get("connectivity", "")).strip().lower() == "local"
        and str(rx_device.get("serial_port", "")).strip()
        == str(tx_device.get("serial_port", "")).strip()
        and str(rx_device.get("model_id", "")).strip()
        == str(tx_device.get("model_id", "")).strip()
        and str(rx_device.get("baud", "")).strip()
        == str(tx_device.get("baud", "")).strip()
    )
    if rx_device_id != tx_device_id and not same_local_endpoint:
        return
    shared_capable = (
        str(rx_device.get("capability_shared", "")).strip().lower() == "true"
        and str(tx_device.get("capability_shared", "")).strip().lower() == "true"
    )
    if not shared_capable:
        raise ValueError(
            "The selected CAT device cannot be assigned to both RX and TX. "
            "Save the device and use a shared-capable local radio for dual-role operation."
        )
    rx_target = str(settings.get("rx", {}).get("target_vfo", "")).strip().upper()
    tx_target = str(settings.get("tx", {}).get("target_vfo", "")).strip().upper()
    if not rx_target or rx_target == "CURRENT" or not tx_target or tx_target == "CURRENT":
        raise ValueError(
            "A shared CAT device requires explicit, different RX and TX targets."
        )
    semantic_targets = {
        "VFOA": "MAIN",
        "VFOB": "SUB",
        "MAINA": "MAIN",
        "MAINB": "MAIN",
        "SUBA": "SUB",
        "SUBB": "SUB",
    }
    if semantic_targets.get(rx_target, rx_target) == semantic_targets.get(
        tx_target,
        tx_target,
    ):
        raise ValueError("A shared CAT device cannot use the same target for RX and TX.")
    available_targets = {
        target.strip().upper()
        for target in str(
            rx_device.get("capability_targets", "")
        ).split(",")
        if target.strip()
    }
    if available_targets and (
        rx_target not in available_targets or tx_target not in available_targets
    ):
        raise ValueError(
            "The selected shared RX/TX target is not in the device's verified target list."
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
    _append_keys(lines, values, ["https_enabled", "https_port", "tls_certfile", "tls_keyfile", "tls_names"])

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
        lines.append("# Assign a configured device from the My Devices inventory.")
        lines.append(f"device_id = {values['device_id']}")
        lines.append(f"# Select which Hamlib target should control {role.upper()}.")
        lines.append(f"target_vfo = {values['target_vfo']}")
        if role == "tx":
            lines.append(
                "# When RX and TX share the same local radio, enable rig split mode for TX updates."
            )
            lines.append(f"shared_local_split_mode = {values['shared_local_split_mode']}")
        lines.append("# Enable verbose CAT/Hamlib command logging for this role.")
        lines.append(f"cat_debug_logging = {values['cat_debug_logging']}")

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
    values = section("icom")
    lines.append("# Native IC-9700 control over the LAN transport; one Pi-Sat process owns it.")
    _append_keys(lines, values, SETTINGS_SCHEMA["icom"])
    lines.append("")
    values = section("aprs")
    lines.append("# APRS transmit. Dire Wolf's gen_packets modulates the frame and")
    lines.append("# Pi-Sat sends it through the native radio. An empty mycall disables transmit.")
    lines.append("# channel selects the decoder side: main (left) or right, or both.")
    _append_keys(lines, values, SETTINGS_SCHEMA["aprs"])
    lines.append("")
    values = section("sstv")
    lines.append("# Level trim in dB for the copy of the receive audio handed to the")
    lines.append("# SSTV decoder. It never changes the radio or the browser audio.")
    _append_keys(lines, values, SETTINGS_SCHEMA["sstv"])
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
            "state_updates": device.state_updates,
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
            "capability_targets": _load_cat_device_metadata(
                parser, device.device_id, "capability_targets"
            ),
            "capability_last_test_utc": _load_cat_device_metadata(
                parser, device.device_id, "capability_last_test_utc"
            ),
            "capability_notes": _load_cat_device_metadata(
                parser, device.device_id, "capability_notes"
            ),
            "capability_async": _load_cat_device_metadata(
                parser, device.device_id, "capability_async"
            ),
            "capability_async_version": _load_cat_device_metadata(
                parser, device.device_id, "capability_async_version"
            ),
            "capability_async_properties": _load_cat_device_metadata(
                parser, device.device_id, "capability_async_properties"
            ),
            "capability_async_notes": _load_cat_device_metadata(
                parser, device.device_id, "capability_async_notes"
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
            state_updates=_normalize_state_updates(
                parser.get(section, "state_updates", fallback="automatic")
            ),
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
                    state_updates=existing.state_updates,
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
            state_updates=_normalize_state_updates(
                parser.get(role, "state_updates", fallback="automatic")
            ),
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


def _normalize_cat_devices(
    cat_devices: list[dict[str, Any]],
) -> tuple[list[dict[str, str]], dict[str, str]]:
    normalized: list[dict[str, str]] = []
    id_mapping: dict[str, str] = {}
    seen_ids: set[str] = set()
    for index, raw_device in enumerate(cat_devices, start=1):
        raw_device_id = str(raw_device.get("device_id", "")).strip()
        device_id = normalize_cat_device_id(raw_device_id, f"cat-device-{index}")
        if device_id == NATIVE_ICOM_DEVICE_ID:
            raise ValueError(
                f"{NATIVE_ICOM_DEVICE_ID} is reserved for Pi-Sat's native IC-9700 controller."
            )
        if raw_device_id in id_mapping:
            raise ValueError(f"Duplicate CAT device ID: {raw_device_id}")
        if device_id in seen_ids:
            raise ValueError(
                f"Duplicate CAT device ID after normalization: {device_id}"
            )
        seen_ids.add(device_id)
        if raw_device_id:
            id_mapping[raw_device_id] = device_id
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
                "state_updates": _normalize_state_updates(
                    raw_device.get("state_updates", "automatic")
                ),
                "capability_comm": stringify_capability_value(raw_device.get("capability_comm")),
                "capability_ptt": stringify_capability_value(raw_device.get("capability_ptt")),
                "capability_vfo": stringify_capability_value(raw_device.get("capability_vfo")),
                "capability_shared": stringify_capability_value(raw_device.get("capability_shared")),
                "capability_targets": str(raw_device.get("capability_targets", "")).strip(),
                "capability_last_test_utc": str(raw_device.get("capability_last_test_utc", "")).strip(),
                "capability_notes": str(raw_device.get("capability_notes", "")).strip(),
                "capability_async": str(raw_device.get("capability_async", "")).strip(),
                "capability_async_version": str(raw_device.get("capability_async_version", "")).strip(),
                "capability_async_properties": str(raw_device.get("capability_async_properties", "")).strip(),
                "capability_async_notes": str(raw_device.get("capability_async_notes", "")).strip(),
            }
        )
    return normalized, id_mapping


def normalize_cat_device_id(value: Any, fallback: str = "") -> str:
    device_id = str(value or "").strip().lower()
    device_id = "".join(
        character if character.isalnum() or character in {"-", "_"} else "-"
        for character in device_id
    ).strip("-_")
    return device_id or fallback


def _normalize_state_updates(value: Any) -> str:
    return "polling" if str(value or "").strip().lower() == "polling" else "automatic"


def parse_int_value(value: Any, fallback: int) -> int:
    if value is None:
        return fallback
    text = str(value).strip()
    if not text:
        return fallback
    return int(text)


def parse_float_value(value: Any, fallback: float) -> float:
    if value is None:
        return fallback
    text = str(value).strip()
    if not text:
        return fallback
    return float(text)


def stringify_optional_int(value: Any) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    if not text:
        return ""
    return str(int(text))


def stringify_capability_value(value: Any) -> str:
    text = str(value).strip().lower()
    if text not in {"true", "false"}:
        return ""
    return text
