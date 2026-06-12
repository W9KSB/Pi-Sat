from __future__ import annotations

from collections.abc import Callable
from threading import Lock

from fastapi import FastAPI


def register_system_api(
    app: FastAPI,
    *,
    get_monitor_entries: Callable[[], list[dict[str, object]]],
    monitor_log_lock: Lock,
    build_status: Callable[[], dict[str, object]],
    get_hamlib_radio_models_payload: Callable[[], dict[str, object]],
    get_hamlib_rotator_models_payload: Callable[[], dict[str, object]],
) -> None:
    @app.get("/api/monitor/logs")
    def get_monitor_logs() -> dict[str, object]:
        with monitor_log_lock:
            entries = list(get_monitor_entries())
        return {"entries": entries}

    @app.get("/api/status")
    def get_status() -> dict[str, object]:
        return build_status()

    @app.get("/api/hamlib/radio-models")
    def get_hamlib_radio_models() -> dict[str, object]:
        return get_hamlib_radio_models_payload()

    @app.get("/api/hamlib/rotator-models")
    def get_hamlib_rotator_models() -> dict[str, object]:
        return get_hamlib_rotator_models_payload()
