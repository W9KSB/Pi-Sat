from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from queue import Empty
from typing import Any

from fastapi import Body, FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, Response

LOGGER = logging.getLogger(__name__)


def register_sstv_api(
    app: FastAPI,
    *,
    get_manager: Callable[[], Any],
    save_rx_gain: Callable[[float], None] | None = None,
) -> None:
    @app.get("/api/sstv")
    def get_sstv() -> dict[str, Any]:
        return get_manager().snapshot()

    @app.post("/api/sstv/enabled")
    def set_sstv_enabled(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
        try:
            return get_manager().set_enabled(payload["enabled"])
        except (KeyError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/api/sstv/current")
    def get_current_sstv() -> Response:
        png = get_manager().current_png()
        if png is None:
            raise HTTPException(status_code=404, detail="No SSTV image is being decoded")
        return Response(content=png, media_type="image/png", headers={"Cache-Control": "no-store"})

    @app.get("/api/sstv/images")
    def list_sstv_images() -> list[dict[str, Any]]:
        return get_manager().gallery.list()

    @app.post("/api/sstv/decoder-level")
    def set_sstv_decoder_level(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
        """Trim the level of the audio fed to the decoder (module stream only)."""
        try:
            snapshot = get_manager().set_rx_gain_db(payload["gain_db"])
        except (KeyError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if save_rx_gain is not None:
            try:
                save_rx_gain(snapshot["rx_gain_db"])
            except Exception:  # Persisting must never reject a live adjustment.
                LOGGER.warning("Could not persist the SSTV decoder input level", exc_info=True)
        return snapshot

    @app.get("/api/sstv/images/{capture_id}")
    def get_sstv_image(capture_id: str) -> FileResponse:
        path = get_manager().gallery.image_path(capture_id)
        if path is None:
            raise HTTPException(status_code=404, detail="SSTV image not found")
        return FileResponse(path, media_type="image/png", headers={"Cache-Control": "no-store"})

    @app.get("/api/sstv/images/{capture_id}/download")
    def download_sstv_image(capture_id: str) -> FileResponse:
        gallery = get_manager().gallery
        metadata = gallery.find(capture_id)
        path = gallery.image_path(capture_id)
        if metadata is None or path is None:
            raise HTTPException(status_code=404, detail="SSTV image not found")
        return FileResponse(path, media_type="image/png", filename=str(metadata["filename"]))

    @app.websocket("/api/sstv/events")
    async def sstv_events(websocket: WebSocket) -> None:
        await websocket.accept()
        manager = get_manager()
        subscriber = manager.subscribe()
        try:
            await websocket.send_json({"type": "status", "status": manager.snapshot()})
            while True:
                try:
                    event = await asyncio.to_thread(subscriber.get, True, 1.0)
                except Empty:
                    await websocket.send_json({"type": "heartbeat"})
                    continue
                await websocket.send_json(event)
        except WebSocketDisconnect:
            return
        finally:
            manager.unsubscribe(subscriber)
