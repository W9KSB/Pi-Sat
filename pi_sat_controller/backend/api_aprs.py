from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from queue import Empty
from typing import Any

from fastapi import Body, FastAPI, HTTPException, WebSocket, WebSocketDisconnect

LOGGER = logging.getLogger(__name__)


def register_aprs_api(
    app: FastAPI,
    *,
    get_manager: Callable[[], Any],
    get_transmitter: Callable[[], Any],
    save_rx_gain: Callable[[float], None] | None = None,
) -> None:
    @app.get("/api/aprs")
    def get_aprs() -> dict[str, Any]:
        return {**get_manager().snapshot(), "transmit": get_transmitter().snapshot()}

    @app.post("/api/aprs/enabled")
    def set_aprs_enabled(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
        try:
            return get_manager().set_enabled(payload["enabled"])
        except (KeyError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/api/aprs/packets")
    def list_aprs_packets() -> list[dict[str, Any]]:
        return get_manager().log.list()

    @app.get("/api/aprs/decoder-log")
    def get_aprs_decoder_log() -> dict[str, Any]:
        return {"lines": get_manager().decoder_log()}

    @app.post("/api/aprs/decoder-level")
    def set_aprs_decoder_level(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
        """Trim the level of the audio fed to the decoder (module stream only)."""
        try:
            snapshot = get_manager().set_rx_gain_db(payload["gain_db"])
        except (KeyError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if save_rx_gain is not None:
            try:
                save_rx_gain(snapshot["rx_gain_db"])
            except Exception:  # Persisting must never reject a live adjustment.
                LOGGER.warning("Could not persist the APRS decoder input level", exc_info=True)
        return snapshot

    @app.post("/api/aprs/transmit/arm")
    def set_aprs_arm(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
        try:
            return get_transmitter().set_armed(payload["armed"])
        except (KeyError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/aprs/transmit")
    def transmit_aprs(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
        try:
            status = get_transmitter().transmit(
                kind=str(payload.get("kind", "beacon")),
                text=str(payload.get("text", "")),
                addressee=str(payload.get("addressee", "")),
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc
        return {"ok": True, "status": status}

    @app.websocket("/api/aprs/events")
    async def aprs_events(websocket: WebSocket) -> None:
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
