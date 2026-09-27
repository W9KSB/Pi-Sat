from __future__ import annotations

from collections.abc import Callable
from typing import Any

from fastapi import FastAPI


def register_audio_stream_api(
    app: FastAPI,
    *,
    get_status: Callable[[], dict[str, Any]],
) -> None:
    """Expose read-only status for the passive RX audio listener."""

    @app.get("/api/audio-stream")
    def get_audio_stream_status() -> dict[str, Any]:
        return get_status()
