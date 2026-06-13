from __future__ import annotations

from collections.abc import Callable
from typing import Any

from fastapi import Body, FastAPI, HTTPException


def register_settings_api(
    app: FastAPI,
    *,
    logger,
    settings_schema: dict[str, list[str]],
    load_settings: Callable[[], dict[str, dict[str, str]]],
    save_settings: Callable[[dict[str, Any]], None],
    reload_runtime_config: Callable[[], None],
    reload_rotator_config_only: Callable[[], None],
    list_serial_devices: Callable[[], list[dict[str, str]]],
    run_device_test: Callable[[str, dict[str, Any]], dict[str, object]],
    list_automation_scripts: Callable[[], list[dict[str, str]]],
    run_automation_script_test: Callable[[str, str], dict[str, object]],
    build_status: Callable[[], dict[str, object]],
) -> None:
    @app.get("/api/settings")
    def get_settings() -> dict[str, object]:
        return {
            "schema": settings_schema,
            "settings": load_settings(),
        }

    @app.post("/api/settings")
    def update_settings(payload: dict[str, Any] = Body(...)) -> dict[str, object]:
        try:
            save_settings(payload.get("settings", {}))
            reload_runtime_config()
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {
            "schema": settings_schema,
            "settings": load_settings(),
        }

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
            return run_device_test(normalized_role, overrides)
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
