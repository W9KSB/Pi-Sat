from __future__ import annotations

import logging
from dataclasses import replace
from typing import Any

from pi_sat_controller.backend.config import DeviceConfig, load_settings
from pi_sat_controller.backend.radio.hamlib_client import HamlibClient
from pi_sat_controller.backend.radio.hamlib_models import load_hamlib_radio_models
from pi_sat_controller.backend.radio.local_hamlib_client import LocalHamlibClient
from pi_sat_controller.backend.radio.radio_manager import RadioManager
from pi_sat_controller.backend.rotator.hamlib_rotator_models import (
    load_hamlib_rotator_models,
)
from pi_sat_controller.backend.rotator.local_rotctld_client import LocalRotctldClient
from pi_sat_controller.backend.rotator.rotctld_client import RotctldClient
from pi_sat_controller.backend.sdr.polling_sdr import (
    PollingRadioFrequencyManager,
    PollingSdrManager,
)


def parse_bool_setting(value: Any, fallback: bool = False) -> bool:
    text = str(value).strip().lower()
    if not text or text in {"none", "null"}:
        return fallback
    return text in {"1", "yes", "true", "on"}


def parse_int_setting(value: Any, fallback: int | None = None) -> int | None:
    text = str(value).strip()
    if not text or text.lower() in {"none", "null"}:
        return fallback
    return int(text)


def parse_float_setting(value: Any, fallback: float | None = None) -> float | None:
    text = str(value).strip()
    if not text or text.lower() in {"none", "null"}:
        return fallback
    return float(text)


def device_config_from_settings(role: str, overrides: dict[str, Any]) -> DeviceConfig:
    settings = load_settings()
    section_settings = dict(settings.get(role, {}))
    section_settings.update(
        {str(key): "" if value is None else str(value) for key, value in overrides.items()}
    )
    return DeviceConfig(
        enabled=parse_bool_setting(section_settings.get("enabled"), False),
        connectivity=str(section_settings.get("connectivity", "network")).strip() or "network",
        host=str(section_settings.get("host", "")).strip(),
        port=parse_int_setting(section_settings.get("port"), 0) or 0,
        serial_port=str(section_settings.get("serial_port", "")).strip(),
        baud=parse_int_setting(section_settings.get("baud")),
        model_id=parse_int_setting(section_settings.get("model_id")),
        target_vfo=str(section_settings.get("target_vfo", "")).strip() or None,
        write_enabled=parse_bool_setting(section_settings.get("write_enabled"), False),
        timeout_s=parse_float_setting(section_settings.get("timeout_s"), 2.0) or 2.0,
        cat_debug_logging=parse_bool_setting(
            section_settings.get("cat_debug_logging"),
            False,
        ),
        min_elevation_deg=parse_float_setting(section_settings.get("min_elevation_deg")),
        home_azimuth_deg=parse_float_setting(section_settings.get("home_azimuth_deg")),
        home_elevation_deg=parse_float_setting(section_settings.get("home_elevation_deg")),
        return_home_after_pass=parse_bool_setting(
            section_settings.get("return_home_after_pass"),
            False,
        ),
    )


def device_endpoint_details(role: str, device_config: DeviceConfig) -> dict[str, object]:
    details: dict[str, object] = {
        "connectivity": device_config.connectivity,
        "timeout_s": device_config.timeout_s,
    }
    if device_config.connectivity == "network":
        details["host"] = device_config.host
        details["port"] = device_config.port
    else:
        details["serial_port"] = device_config.serial_port
        details["baud"] = device_config.baud
        details["model_id"] = device_config.model_id
        if role in {"rx", "tx"}:
            details["target_vfo"] = device_config.target_vfo or "current"
    return details


def build_radio_client(device_config, role: str, shared_local_client=None):
    if device_config.connectivity == "network":
        return HamlibClient(
            host=device_config.host,
            port=device_config.port,
            timeout_s=device_config.timeout_s,
            target_vfo=device_config.target_vfo,
            debug_logging=device_config.cat_debug_logging,
            role_label=role.lower(),
        )
    if device_config.connectivity == "local":
        if not device_config.model_id:
            raise ValueError(f"{role} model_id is required for local CAT control")
        if not device_config.serial_port:
            raise ValueError(f"{role} serial_port is required for local CAT control")
        if not device_config.baud:
            raise ValueError(f"{role} baud is required for local CAT control")
        if shared_local_client is not None:
            return shared_local_client
        return LocalHamlibClient(
            model_id=device_config.model_id,
            serial_port=device_config.serial_port,
            baud=device_config.baud,
            timeout_s=device_config.timeout_s,
            target_vfo=device_config.target_vfo,
            debug_logging=device_config.cat_debug_logging,
            role_label=role.lower(),
        )
    raise ValueError(f"Unsupported TX connectivity: {device_config.connectivity}")


def build_rotator_client(device_config):
    if device_config.connectivity == "network":
        return RotctldClient(
            host=device_config.host,
            port=device_config.port,
            timeout_s=device_config.timeout_s,
            debug_logging=device_config.cat_debug_logging,
            role_label="rotator",
        )
    if device_config.connectivity == "local":
        if not device_config.model_id:
            raise ValueError("Rotator model_id is required for local control")
        if not device_config.serial_port:
            raise ValueError("Rotator serial_port is required for local control")
        if not device_config.baud:
            raise ValueError("Rotator baud is required for local control")
        return LocalRotctldClient(
            model_id=device_config.model_id,
            serial_port=device_config.serial_port,
            baud=device_config.baud,
            timeout_s=device_config.timeout_s,
            debug_logging=device_config.cat_debug_logging,
            role_label="rotator",
        )
    raise ValueError(f"Unsupported rotator connectivity: {device_config.connectivity}")


def build_rx_manager(
    device_config,
    shared_local_client=None,
    failure_threshold: int = 3,
):
    if device_config.connectivity == "network":
        return PollingSdrManager(
            host=device_config.host,
            port=device_config.port,
            timeout_s=device_config.timeout_s,
            poll_interval_s=1.0,
            debug_logging=device_config.cat_debug_logging,
            failure_threshold=failure_threshold,
        )
    if device_config.connectivity == "local":
        client = build_radio_client(device_config, "RX", shared_local_client)
        return PollingRadioFrequencyManager(
            radio_manager=RadioManager(
                client=client,
                enabled=device_config.enabled,
                write_enabled=device_config.write_enabled,
                target_vfo=device_config.target_vfo,
                failure_threshold=failure_threshold,
            ),
            poll_interval_s=1.0,
        )
    raise ValueError(f"Unsupported RX connectivity: {device_config.connectivity}")


def run_device_test(
    role: str,
    overrides: dict[str, Any],
    logger: logging.Logger,
) -> dict[str, object]:
    device_config = device_config_from_settings(role, overrides)
    device_config = replace(
        device_config,
        timeout_s=min(max(float(device_config.timeout_s), 0.5), 5.0),
    )
    details = device_endpoint_details(role, device_config)
    client = None
    try:
        if role in {"rx", "tx"}:
            client = build_radio_client(device_config, role.upper())
            frequency_hz = client.get_frequency()
            details["frequency_hz"] = frequency_hz
            return {
                "ok": True,
                "role": role,
                "message": f"{role.upper()} test succeeded.",
                "details": details,
            }

        client = build_rotator_client(device_config)
        position = client.get_position()
        details["azimuth_deg"] = position.azimuth_deg
        details["elevation_deg"] = position.elevation_deg
        return {
            "ok": True,
            "role": role,
            "message": "Rotator test succeeded.",
            "details": details,
        }
    except Exception as exc:
        logger.warning("Device test failed role=%s error=%s", role, exc)
        details["error"] = str(exc)
        return {
            "ok": False,
            "role": role,
            "message": f"{role.upper() if role != 'rotator' else 'Rotator'} test failed.",
            "details": details,
        }
    finally:
        if client is not None and hasattr(client, "close"):
            try:
                client.close()
            except Exception:
                logger.exception("Temporary device test client cleanup failed role=%s", role)


def load_hamlib_model_caches(
    logger: logging.Logger,
) -> tuple[list[dict[str, object]], str | None, list[dict[str, object]], str | None]:
    try:
        radio_models = [model.to_dict() for model in load_hamlib_radio_models()]
        radio_error = None
    except FileNotFoundError:
        radio_models = []
        radio_error = "Hamlib radio models are unavailable on this system."
    except Exception:
        radio_models = []
        logger.exception("Unable to load Hamlib radio models")
        radio_error = "Hamlib radio models could not be loaded."

    try:
        rotator_models = [model.to_dict() for model in load_hamlib_rotator_models()]
        rotator_error = None
    except FileNotFoundError:
        rotator_models = []
        rotator_error = "Hamlib rotator models are unavailable on this system."
    except Exception:
        rotator_models = []
        logger.exception("Unable to load Hamlib rotator models")
        rotator_error = "Hamlib rotator models could not be loaded."

    return radio_models, radio_error, rotator_models, rotator_error


def uses_same_local_radio(config) -> bool:
    return (
        config.rx.enabled
        and config.tx.enabled
        and config.rx.connectivity == "local"
        and config.tx.connectivity == "local"
        and bool(config.rx.serial_port)
        and config.rx.serial_port == config.tx.serial_port
        and config.rx.model_id == config.tx.model_id
        and config.rx.baud == config.tx.baud
    )
