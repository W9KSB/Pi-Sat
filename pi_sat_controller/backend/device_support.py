from __future__ import annotations

import logging
from dataclasses import replace
from datetime import datetime, timezone
from typing import Any

from pi_sat_controller.backend.config import DeviceConfig, load_cat_devices, load_settings
from pi_sat_controller.backend.radio.hamlib_client import HamlibClient
from pi_sat_controller.backend.radio.hamlib_models import load_hamlib_radio_models
from pi_sat_controller.backend.radio.local_hamlib_client import LocalHamlibClient
from pi_sat_controller.backend.radio.radio_manager import RadioManager, normalize_hamlib_vfo
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


def _device_config_from_cat_device_entry(device_settings: dict[str, Any]) -> DeviceConfig:
    return DeviceConfig(
        enabled=True,
        device_id=str(device_settings.get("device_id", "")).strip() or None,
        connectivity=str(device_settings.get("connectivity", "network")).strip() or "network",
        host=str(device_settings.get("host", "")).strip(),
        port=parse_int_setting(device_settings.get("port"), 0) or 0,
        serial_port=str(device_settings.get("serial_port", "")).strip(),
        baud=parse_int_setting(device_settings.get("baud")),
        model_id=parse_int_setting(device_settings.get("model_id")),
        target_vfo=None,
        shared_local_split_mode=False,
        write_enabled=True,
        timeout_s=parse_float_setting(device_settings.get("timeout_s"), 2.0) or 2.0,
        state_updates=str(device_settings.get("state_updates", "automatic")).strip()
        or "automatic",
        cat_debug_logging=False,
    )


def device_config_from_settings(
    role: str,
    overrides: dict[str, Any],
    cat_device_overrides: list[dict[str, Any]] | None = None,
) -> DeviceConfig:
    settings = load_settings()
    section_settings = dict(settings.get(role, {}))
    section_settings.update(
        {str(key): "" if value is None else str(value) for key, value in overrides.items()}
    )
    cat_devices = {
        str(device["device_id"]): device
        for device in (cat_device_overrides if cat_device_overrides is not None else load_cat_devices())
        if str(device.get("device_id", "")).strip()
    }
    base_device = cat_devices.get(str(section_settings.get("device_id", "")).strip())
    base_device_config = (
        _device_config_from_cat_device_entry(base_device) if base_device is not None else None
    )
    return DeviceConfig(
        enabled=parse_bool_setting(section_settings.get("enabled"), False),
        device_id=str(section_settings.get("device_id", "")).strip() or None,
        connectivity=(
            base_device_config.connectivity
            if base_device_config is not None
            else str(section_settings.get("connectivity", "network")).strip() or "network"
        ),
        host=(
            base_device_config.host
            if base_device_config is not None
            else str(section_settings.get("host", "")).strip()
        ),
        port=(
            base_device_config.port
            if base_device_config is not None
            else parse_int_setting(section_settings.get("port"), 0) or 0
        ),
        serial_port=(
            base_device_config.serial_port
            if base_device_config is not None
            else str(section_settings.get("serial_port", "")).strip()
        ),
        baud=(
            base_device_config.baud
            if base_device_config is not None
            else parse_int_setting(section_settings.get("baud"))
        ),
        model_id=(
            base_device_config.model_id
            if base_device_config is not None
            else parse_int_setting(section_settings.get("model_id"))
        ),
        target_vfo=str(section_settings.get("target_vfo", "")).strip() or None,
        shared_local_split_mode=parse_bool_setting(
            section_settings.get("shared_local_split_mode"),
            False,
        ),
        write_enabled=True,
        timeout_s=(
            base_device_config.timeout_s
            if base_device_config is not None
            else parse_float_setting(section_settings.get("timeout_s"), 2.0) or 2.0
        ),
        state_updates=(
            base_device_config.state_updates
            if base_device_config is not None
            else str(section_settings.get("state_updates", "automatic")).strip()
            or "automatic"
        ),
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
        "state_updates": device_config.state_updates,
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
        if role.strip().lower() == "tx":
            raise ValueError("TX currently supports local devices only.")
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
        target_vfo = normalize_hamlib_vfo(device_config.target_vfo)
        return LocalHamlibClient(
            model_id=device_config.model_id,
            serial_port=device_config.serial_port,
            baud=device_config.baud,
            timeout_s=device_config.timeout_s,
            target_vfo=target_vfo,
            debug_logging=device_config.cat_debug_logging,
            role_label=role.lower(),
            vfo_mode=target_vfo is not None,
            state_updates=device_config.state_updates,
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
                poll_target_vfo=False,
            ),
            poll_interval_s=1.0,
        )
    raise ValueError(f"Unsupported RX connectivity: {device_config.connectivity}")


def run_device_test(
    role: str,
    overrides: dict[str, Any],
    logger: logging.Logger,
    cat_device_overrides: list[dict[str, Any]] | None = None,
) -> dict[str, object]:
    device_config = device_config_from_settings(role, overrides, cat_device_overrides)
    if role == "tx" and device_config.connectivity != "local":
        raise ValueError("TX device test is only available for local devices.")
    device_config = replace(
        device_config,
        timeout_s=min(max(float(device_config.timeout_s), 0.5), 5.0),
    )
    details = device_endpoint_details(role, device_config)
    client = None
    try:
        if role in {"rx", "tx"}:
            client = build_radio_client(device_config, role.upper())
            target_vfo = normalize_hamlib_vfo(device_config.target_vfo)
            frequency_hz = (
                client.get_frequency_on_vfo(target_vfo)
                if target_vfo and hasattr(client, "get_frequency_on_vfo")
                else client.get_frequency()
            )
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


def run_cat_device_test(
    device_settings: dict[str, Any],
    logger: logging.Logger,
) -> dict[str, object]:
    device_config = replace(
        _device_config_from_cat_device_entry(device_settings),
        timeout_s=min(
            max(float(parse_float_setting(device_settings.get("timeout_s"), 2.0) or 2.0), 0.5),
            5.0,
        ),
    )
    details = device_endpoint_details("rx", device_config)
    client = None
    try:
        caps_output = _load_hamlib_caps_output(device_config)
        capability_targets = _parse_capability_targets_from_caps(caps_output)
        if device_config.connectivity == "local" and len(capability_targets) >= 2:
            client = LocalHamlibClient(
                model_id=device_config.model_id or 0,
                serial_port=device_config.serial_port,
                baud=device_config.baud or 0,
                timeout_s=device_config.timeout_s,
                target_vfo=capability_targets[0],
                debug_logging=device_config.cat_debug_logging,
                role_label="cat-device-test",
                vfo_mode=True,
                state_updates=device_config.state_updates,
            )
            client.ensure_connected()
        else:
            client = build_radio_client(device_config, "CAT Device")
        vfo_test_target = (
            capability_targets[0]
            if isinstance(client, LocalHamlibClient) and client.vfo_mode
            else None
        )
        frequency_hz = (
            client.get_frequency_on_vfo(vfo_test_target)
            if vfo_test_target
            else client.get_frequency()
        )
        details["frequency_hz"] = frequency_hz
        capability_comm = True
        capability_ptt = _probe_capability(
            lambda: client.get_ptt_on_vfo(vfo_test_target)
            if vfo_test_target
            else client.get_ptt()
        )
        verified_targets: list[str] = []
        target_frequencies: dict[str, int] = {}
        if (
            device_config.connectivity == "local"
            and len(capability_targets) >= 2
            and isinstance(client, LocalHamlibClient)
        ):
            for target in capability_targets:
                try:
                    target_frequency = client.get_frequency_on_vfo(target)
                    client.set_frequency_on_vfo(target, target_frequency)
                    readback = client.get_frequency_on_vfo(target)
                    if abs(readback - target_frequency) > 100:
                        raise RuntimeError(
                            f"{target} write/read verification failed: "
                            f"wrote {target_frequency}, read {readback}"
                        )
                except Exception as exc:
                    logger.debug(
                        "CAT device target probe failed target=%s error=%s",
                        target,
                        exc,
                    )
                    continue
                verified_targets.append(target)
                target_frequencies[target] = readback
        capability_vfo = len(verified_targets) >= 2
        capability_shared = (
            device_config.connectivity == "local"
            and capability_ptt
            and capability_vfo
        )
        if target_frequencies:
            details["target_frequencies_hz"] = target_frequencies
        details["capability_comm"] = capability_comm
        details["capability_ptt"] = capability_ptt
        details["capability_vfo"] = capability_vfo
        details["capability_shared"] = capability_shared
        details["capability_targets"] = ",".join(
            verified_targets if verified_targets else capability_targets
        )
        details["capability_last_test_utc"] = datetime.now(timezone.utc).isoformat()
        details["capability_notes"] = _build_capability_notes(
            capability_comm,
            capability_ptt,
            capability_vfo,
            capability_shared,
            device_config.connectivity,
        )
        if isinstance(client, LocalHamlibClient):
            async_status = client.async_status()
            details["capability_async"] = async_status["state"]
            details["capability_async_version"] = async_status.get("hamlib_version") or ""
            details["capability_async_properties"] = ",".join(
                str(value) for value in async_status.get("verified_properties", [])
            )
            details["capability_async_notes"] = _build_async_capability_notes(async_status)
            logger.info(
                "CAT device test Hamlib version=%s async_state=%s backend_supported=%s listener_running=%s verified_properties=%s",
                async_status.get("hamlib_version"),
                async_status.get("state"),
                async_status.get("backend_supported"),
                async_status.get("listener_running"),
                async_status.get("verified_properties"),
            )
        else:
            details["capability_async"] = "unsupported"
            details["capability_async_version"] = ""
            details["capability_async_properties"] = ""
            details["capability_async_notes"] = (
                "Polling is used for external rigctld endpoints because Pi-Sat does not own their async configuration."
            )
        return {
            "ok": True,
            "message": "Radio connected successfully.",
            "details": details,
        }
    except Exception as exc:
        logger.warning("CAT device test failed error=%s", exc)
        details["error"] = str(exc)
        details["capability_comm"] = False
        details["capability_ptt"] = False
        details["capability_vfo"] = False
        details["capability_shared"] = False
        details["capability_targets"] = ""
        details["capability_last_test_utc"] = datetime.now(timezone.utc).isoformat()
        details["capability_notes"] = "Communication failed."
        details["capability_async"] = "unsupported"
        details["capability_async_version"] = ""
        details["capability_async_properties"] = ""
        details["capability_async_notes"] = "Async capability could not be tested because radio communication failed."
        return {
            "ok": False,
            "message": "Capability test failed.",
            "details": details,
        }
    finally:
        if client is not None and hasattr(client, "close"):
            try:
                client.close()
            except Exception:
                logger.exception("Temporary CAT device test client cleanup failed")


def _probe_capability(probe) -> bool:
    try:
        probe()
        return True
    except Exception as exc:
        message = str(exc).lower()
        if (
            "feature not available" in message
            or "rprt -" in message
            or "rejected ptt read" in message
            or "rejected vfo read" in message
        ):
            return False
        raise


def _build_capability_notes(
    capability_comm: bool,
    capability_ptt: bool,
    capability_vfo: bool,
    capability_shared: bool,
    connectivity: str,
) -> str:
    if not capability_comm:
        return "Communication failed."
    if connectivity != "local":
        return "Network devices are RX-only at this time."
    notes: list[str] = []
    if capability_shared:
        notes.append("Supports shared RX and TX role assignment.")
    if notes:
        return " ".join(notes)
    missing: list[str] = []
    if not capability_ptt:
        missing.append("PTT detection")
    if not capability_vfo:
        missing.append("targetable VFO support")
    if not missing:
        return "Basic CAT communication succeeded."
    return f"Missing {' and '.join(missing)}. Single role capable only."


def _build_async_capability_notes(status: dict[str, object]) -> str:
    preference = str(status.get("preference", "automatic"))
    state = str(status.get("state", "unsupported"))
    if preference == "polling":
        return "Polling Only is selected; real-time pushed updates are disabled."
    if state == "verified":
        properties = ", ".join(str(value) for value in status.get("verified_properties", []))
        return (
            f"Real-time pushed updates verified for {properties or 'radio state'}. "
            "Pi-Sat will keep slow reconciliation polling."
        )
    if state == "available":
        return (
            "Real-time pushed updates are available and the listener started. "
            "Pi-Sat will continue normal polling until a valid async state change is received."
        )
    reason = str(status.get("reason", "")).strip()
    return (
        "Real-time pushed updates are not available for this radio/Hamlib backend. "
        f"Pi-Sat will use polling.{f' {reason}' if reason else ''}"
    )


def _load_hamlib_caps_output(device_config: DeviceConfig) -> str:
    if device_config.connectivity != "local" or not device_config.model_id:
        return ""

    import subprocess

    try:
        result = subprocess.run(
            ["rigctl", "-m", str(device_config.model_id), "-u"],
            capture_output=True,
            check=False,
            text=True,
            timeout=min(max(float(device_config.timeout_s), 1.0), 5.0),
        )
    except Exception:
        return ""

    if result.returncode != 0:
        return ""

    return result.stdout


def _parse_capability_targets_from_caps(caps_output: str) -> list[str]:
    targets: list[str] = []
    seen: set[str] = set()
    for raw_line in caps_output.splitlines():
        line = raw_line.strip()
        if not line.lower().startswith("vfo list:"):
            continue
        for token in line.partition(":")[2].strip().split():
            normalized = normalize_hamlib_vfo(token) or token.strip().upper()
            comparison = normalized.upper()
            if not (
                comparison.startswith("VFO")
                or comparison.startswith("MAIN")
                or comparison.startswith("SUB")
            ):
                continue
            if comparison in seen:
                continue
            seen.add(comparison)
            targets.append(normalized)
    return targets


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
