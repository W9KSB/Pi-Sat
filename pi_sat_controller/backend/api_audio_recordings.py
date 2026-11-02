from __future__ import annotations

from collections.abc import Callable
from typing import Any

from fastapi import Body, FastAPI, HTTPException
from fastapi.responses import FileResponse


def register_audio_recordings_api(
    app: FastAPI,
    *,
    get_manager: Callable[[], Any],
) -> None:
    def recording_response(recording_id: str, *, download: bool) -> FileResponse:
        path = get_manager().recording_path(recording_id)
        if path is None:
            raise HTTPException(status_code=404, detail="Audio recording not found")
        return FileResponse(
            path,
            media_type="audio/wav",
            filename=path.name if download else None,
            headers={"Cache-Control": "no-store"},
        )

    @app.get("/api/audio-recordings/state")
    def audio_recording_state() -> dict[str, Any]:
        return get_manager().snapshot()

    @app.get("/api/audio-recordings")
    def list_audio_recordings() -> dict[str, Any]:
        return {"recordings": get_manager().list()}

    @app.post("/api/audio-recordings/enabled")
    def set_audio_recording_enabled(
        payload: dict[str, Any] = Body(...),
    ) -> dict[str, Any]:
        try:
            return get_manager().set_enabled(payload["enabled"])
        except (KeyError, ValueError, OSError, RuntimeError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/api/audio-recordings/{recording_id}/audio")
    def play_audio_recording(recording_id: str) -> FileResponse:
        return recording_response(recording_id, download=False)

    @app.get("/api/audio-recordings/{recording_id}/download")
    def download_audio_recording(recording_id: str) -> FileResponse:
        return recording_response(recording_id, download=True)
