from __future__ import annotations

from collections.abc import Callable
from typing import Any

from fastapi import Body, FastAPI, HTTPException


def register_audio_stream_api(
    app: FastAPI,
    *,
    get_status: Callable[[], dict[str, Any]],
    set_enabled: Callable[[bool], dict[str, Any]],
) -> None:
    """Expose External DATA Decode status and its enable control."""

    @app.get("/api/audio-stream")
    def get_audio_stream_status() -> dict[str, Any]:
        return get_status()

    @app.post("/api/audio-stream/enabled")
    def set_audio_stream_enabled(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
        if type(payload.get("enabled")) is not bool:
            raise HTTPException(status_code=400, detail="enabled must be a boolean")
        try:
            return set_enabled(payload["enabled"])
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
