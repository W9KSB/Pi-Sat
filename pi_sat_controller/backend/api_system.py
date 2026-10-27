from __future__ import annotations

from collections.abc import Callable
from threading import Lock

from fastapi import FastAPI


def register_system_api(
    app: FastAPI,
    *,
    get_monitor_entries: Callable[[], list[dict[str, object]]],
    monitor_log_lock: Lock,
    get_journal_logs: Callable[[int], dict[str, object]] | None = None,
    build_status: Callable[[], dict[str, object]],
    get_hamlib_radio_models_payload: Callable[[], dict[str, object]],
    get_hamlib_rotator_models_payload: Callable[[], dict[str, object]],
) -> None:
    @app.get("/api/monitor/logs")
    def get_monitor_logs(limit: int = 1000) -> dict[str, object]:
        safe_limit = max(1, min(limit, 2000))
        journal_payload = (
            get_journal_logs(safe_limit) if get_journal_logs is not None else {}
        )
        journal_entries = journal_payload.get("entries")
        if isinstance(journal_entries, list) and journal_entries:
            return {
                **journal_payload,
                "entries": journal_entries,
                "limit": safe_limit,
            }
        with monitor_log_lock:
            fallback_entries = list(get_monitor_entries())
        return {
            "entries": fallback_entries,
            "source": "memory",
            "warning": journal_payload.get("warning")
            or (
                "journalctl returned no entries; showing the in-memory fallback."
                if get_journal_logs is not None
                else ""
            ),
            "limit": safe_limit,
        }

    @app.get("/api/status")
    def get_status() -> dict[str, object]:
        return build_status()

    @app.get("/api/hamlib/radio-models")
    def get_hamlib_radio_models() -> dict[str, object]:
        return get_hamlib_radio_models_payload()

    @app.get("/api/hamlib/rotator-models")
    def get_hamlib_rotator_models() -> dict[str, object]:
        return get_hamlib_rotator_models_payload()
