from __future__ import annotations

from collections.abc import Callable
from typing import Any

from fastapi import Body, FastAPI, HTTPException

MANUAL_ONLY_SETTINGS: dict[str, set[str]] = {
    "tx": {"shared_local_split_mode"},
}


def _sanitize_settings_payload(
    settings_schema: dict[str, list[str]],
    settings: dict[str, dict[str, str]],
) -> dict[str, object]:
    schema: dict[str, list[str]] = {}
    sanitized_settings: dict[str, dict[str, str]] = {}
    for section, keys in settings_schema.items():
        hidden = MANUAL_ONLY_SETTINGS.get(section, set())
        schema[section] = [key for key in keys if key not in hidden]
        section_settings = dict(settings.get(section, {}))
        for key in hidden:
            section_settings.pop(key, None)
        sanitized_settings[section] = section_settings
    return {
        "schema": schema,
        "settings": sanitized_settings,
    }


def register_settings_api(
    app: FastAPI,
    *,
    logger,
    settings_schema: dict[str, list[str]],
    load_settings: Callable[[], dict[str, dict[str, str]]],
    load_cat_devices: Callable[[], list[dict[str, str]]],
    save_settings: Callable[[dict[str, Any], list[dict[str, Any]] | None], None],
    reload_runtime_config: Callable[[], None],
    reload_rotator_config_only: Callable[[], None],
    list_serial_devices: Callable[[], list[dict[str, str]]],
    run_device_test: Callable[
        [str, dict[str, Any], list[dict[str, Any]] | None],
        dict[str, object],
    ],
    run_cat_device_test: Callable[[dict[str, Any]], dict[str, object]],
    list_automation_scripts: Callable[[], list[dict[str, str]]],
    run_automation_script_test: Callable[[str, str], dict[str, object]],
    build_status: Callable[[], dict[str, object]],
) -> None:
    @app.get("/api/settings")
    def get_settings() -> dict[str, object]:
        payload = _sanitize_settings_payload(settings_schema, load_settings())
        payload["cat_devices"] = load_cat_devices()
        return payload

    @app.post("/api/settings")
    def update_settings(payload: dict[str, Any] = Body(...)) -> dict[str, object]:
        try:
            cat_devices = payload.get("cat_devices")
            if cat_devices is not None and not isinstance(cat_devices, list):
                raise ValueError("cat_devices must be an array")
            save_settings(payload.get("settings", {}), cat_devices)
            reload_runtime_config()
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        response = _sanitize_settings_payload(settings_schema, load_settings())
        response["cat_devices"] = load_cat_devices()
        return response

    @app.post("/api/runtime/reload")
    def reload_runtime() -> dict[str, object]:
        try:
            reload_runtime_config()
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return build_status()

    @app.get("/api/serial-devices")
    def get_serial_devices() -> dict[str, object]:
        return {"devices": list_serial_devices()}

    @app.get("/api/automation/scripts")
    def get_automation_scripts() -> dict[str, object]:
        return {"scripts": list_automation_scripts()}

    @app.post("/api/automation/test/{event_name}")
    def test_automation_script(
        event_name: str,
        payload: dict[str, Any] = Body(...),
    ) -> dict[str, object]:
        normalized_event = event_name.strip().lower()
        if normalized_event not in {"aos", "los"}:
            raise HTTPException(status_code=404, detail="Unknown automation event")
        try:
            script_name = str(payload.get("script_name", "")).strip()
            return run_automation_script_test(normalized_event, script_name)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except Exception as exc:
            logger.exception(
                "Automation script test failed unexpectedly event=%s",
                normalized_event,
            )
            raise HTTPException(status_code=500, detail=str(exc)) from exc

    @app.post("/api/device-tests/{role}")
    def test_device(role: str, payload: dict[str, Any] = Body(...)) -> dict[str, object]:
        normalized_role = role.strip().lower()
        if normalized_role not in {"rx", "tx", "rotator"}:
            raise HTTPException(status_code=404, detail="Unknown device role")
        try:
            overrides = payload.get("settings", {})
            if not isinstance(overrides, dict):
                raise ValueError("settings must be an object")
            cat_devices = payload.get("cat_devices")
            if cat_devices is not None and not isinstance(cat_devices, list):
                raise ValueError("cat_devices must be an array")
            return run_device_test(normalized_role, overrides, cat_devices)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except Exception:
            logger.exception("Device test failed unexpectedly role=%s", normalized_role)
            return {
                "ok": False,
                "role": normalized_role,
                "message": "Device test failed unexpectedly.",
                "details": {},
            }

    @app.post("/api/cat-devices/test")
    def test_cat_device(payload: dict[str, Any] = Body(...)) -> dict[str, object]:
        try:
            device = payload.get("device", {})
            if not isinstance(device, dict):
                raise ValueError("device must be an object")
            return run_cat_device_test(device)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except Exception:
            logger.exception("CAT device test failed unexpectedly")
            return {
                "ok": False,
                "message": "CAT device test failed unexpectedly.",
                "details": {},
            }

    @app.post("/api/cat-devices")
    def save_cat_device(payload: dict[str, Any] = Body(...)) -> dict[str, object]:
        try:
            raw_device = payload.get("device", {})
            if not isinstance(raw_device, dict):
                raise ValueError("device must be an object")
            original_device_id = str(payload.get("original_device_id", "")).strip()

            cat_devices = load_cat_devices()
            match_id = original_device_id or str(raw_device.get("device_id", "")).strip()
            existing_device = next(
                (
                    dict(device)
                    for device in cat_devices
                    if str(device.get("device_id", "")).strip() == match_id
                ),
                None,
            )
            merged_device = dict(raw_device)
            if existing_device:
                for key in (
                    "capability_comm",
                    "capability_ptt",
                    "capability_vfo",
                    "capability_shared",
                    "capability_last_test_utc",
                    "capability_notes",
                ):
                    if key not in merged_device:
                        merged_device[key] = existing_device.get(key, "")

            next_devices: list[dict[str, Any]] = []
            replaced = False
            for device in cat_devices:
                device_id = str(device.get("device_id", "")).strip()
                if match_id and device_id == match_id:
                    next_devices.append(dict(merged_device))
                    replaced = True
                else:
                    next_devices.append(dict(device))
            if not replaced:
                next_devices.append(dict(merged_device))

            save_settings({}, next_devices)
            refreshed_devices = load_cat_devices()
            saved_device = dict(
                next(
                        device
                        for device in refreshed_devices
                        if str(device.get("device_id", "")).strip()
                        == str(merged_device.get("device_id", "")).strip()
                    )
                )

            capability_result = run_cat_device_test(saved_device)
            message = "Device saved."
            if capability_result.get("ok") and isinstance(
                capability_result.get("details"), dict
            ):
                saved_device.update(capability_result["details"])
                refreshed_devices = [
                    saved_device
                    if str(device.get("device_id", "")).strip()
                    == str(saved_device.get("device_id", "")).strip()
                    else dict(device)
                    for device in refreshed_devices
                ]
                save_settings({}, refreshed_devices)
                refreshed_devices = load_cat_devices()
                saved_device = dict(
                    next(
                        device
                        for device in refreshed_devices
                        if str(device.get("device_id", "")).strip()
                        == str(saved_device.get("device_id", "")).strip()
                    )
                )
                message = "Device saved. Capability check refreshed."
            else:
                cached_capability_exists = any(
                    str(existing_device.get(key, "")).strip()
                    for key in (
                        "capability_comm",
                        "capability_ptt",
                        "capability_vfo",
                        "capability_shared",
                        "capability_last_test_utc",
                    )
                ) if existing_device else False
                if cached_capability_exists:
                    message = (
                        "Device saved. Using last known capability result because the device is not currently reachable."
                    )
                else:
                    message = "Device saved. Capability check could not be refreshed."

            reload_runtime_config()
            return {
                "ok": True,
                "message": message,
                "device": saved_device,
                "cat_devices": refreshed_devices,
            }
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.delete("/api/cat-devices/{device_id}")
    def remove_cat_device(device_id: str) -> dict[str, object]:
        normalized_device_id = device_id.strip()
        if not normalized_device_id:
            raise HTTPException(status_code=400, detail="Device ID is required")
        try:
            settings = load_settings()
            if str(settings.get("rx", {}).get("device_id", "")).strip() == normalized_device_id:
                raise ValueError("Remove the RX role assignment before deleting this device.")
            if str(settings.get("tx", {}).get("device_id", "")).strip() == normalized_device_id:
                raise ValueError("Remove the TX role assignment before deleting this device.")

            cat_devices = load_cat_devices()
            next_devices = [
                dict(device)
                for device in cat_devices
                if str(device.get("device_id", "")).strip() != normalized_device_id
            ]
            save_settings({}, next_devices)
            reload_runtime_config()
            return {
                "ok": True,
                "message": "Device removed.",
                "cat_devices": load_cat_devices(),
            }
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/device-controls")
    def update_device_controls(payload: dict[str, Any] = Body(...)) -> dict[str, object]:
        settings = load_settings()
        rx_changed = False
        tx_changed = False
        rotator_changed = False
        if "rx_enabled" in payload:
            next_value = "true" if bool(payload["rx_enabled"]) else "false"
            rx_changed = settings["rx"]["enabled"] != next_value
            settings["rx"]["enabled"] = next_value
        if "tx_enabled" in payload:
            next_value = "true" if bool(payload["tx_enabled"]) else "false"
            tx_changed = settings["tx"]["enabled"] != next_value
            settings["tx"]["enabled"] = next_value
        if "rotator_enabled" in payload:
            next_value = "true" if bool(payload["rotator_enabled"]) else "false"
            rotator_changed = settings["rotator"]["enabled"] != next_value
            settings["rotator"]["enabled"] = next_value
        try:
            save_settings(settings)
            if rx_changed or tx_changed:
                reload_runtime_config()
            elif rotator_changed:
                reload_rotator_config_only()
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return build_status()
