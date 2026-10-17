from __future__ import annotations

import asyncio
from collections.abc import Callable
from queue import Empty
from typing import Any

from fastapi import Body, FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import Response


def register_wsjtx_api(
    app: FastAPI, *, get_manager: Callable[[], Any],
    get_engine: Callable[[], Any] | None = None,
    get_qso: Callable[[], Any] | None = None,
    get_spectrum: Callable[[], Any] | None = None,
) -> None:
    def snapshot() -> dict[str, Any]:
        result = get_manager().snapshot()
        if get_engine is not None:
            result["engine"] = get_engine().snapshot()
        if get_qso is not None:
            result["qso"] = get_qso().snapshot()
            if get_spectrum is not None:
                result["qso"]["spectrum"] = get_spectrum()
        return result

    def qso_action(method: str, *args: Any) -> dict[str, Any]:
        if get_qso is None:
            raise HTTPException(status_code=503, detail="WSJT-X QSO controls are unavailable")
        try:
            getattr(get_qso(), method)(*args)
        except (ValueError, TypeError) as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        except RuntimeError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        return snapshot()

    @app.get("/api/wsjtx")
    def get_wsjtx() -> dict[str, Any]:
        return snapshot()

    @app.get("/api/wsjtx/decodes")
    def get_wsjtx_decodes() -> list[dict[str, Any]]:
        return get_manager().snapshot()["decodes"]

    @app.post("/api/wsjtx/enabled")
    def set_wsjtx_enabled(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
        if type(payload.get("enabled")) is not bool:
            raise HTTPException(status_code=400, detail="enabled must be a boolean")
        if not payload["enabled"]:
            if get_qso is not None:
                get_qso().set_enabled(False)
            if get_engine is not None:
                get_engine().stop()
        get_manager().set_enabled(payload["enabled"])
        return snapshot()

    @app.post("/api/wsjtx/engine/enabled")
    def set_wsjtx_engine_enabled(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
        if type(payload.get("enabled")) is not bool:
            raise HTTPException(status_code=400, detail="enabled must be a boolean")
        if get_engine is None:
            raise HTTPException(status_code=503, detail="Managed WSJT-X is unavailable")
        if payload["enabled"]:
            listener = get_manager().set_enabled(True)
            if not listener["running"]:
                return snapshot()
            get_engine().start()
        else:
            if get_qso is not None:
                get_qso().set_enabled(False)
            get_engine().stop()
            get_manager().set_enabled(False)
        return snapshot()

    @app.post("/api/wsjtx/reset")
    def reset_wsjtx() -> dict[str, Any]:
        if get_engine is None:
            raise HTTPException(status_code=503, detail="Managed WSJT-X is unavailable")
        # Disable automatic transmit first, then stop the engine. The engine
        # reports any transmit resource it could not release, so the operator
        # never sees a successful reset while PTT or a claim remains.
        if get_qso is not None:
            get_qso().set_enabled(False)
        get_engine().stop()
        get_manager().set_enabled(False)
        return snapshot()

    @app.post("/api/wsjtx/qso/target")
    def set_qso_target(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
        return qso_action("select_target", payload.get("decode_key"))

    @app.post("/api/wsjtx/qso/message")
    def set_qso_message(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
        return qso_action("select_message", payload.get("number"))

    @app.post("/api/wsjtx/qso/offset")
    def set_qso_offset(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
        return qso_action("set_offset", payload.get("hz"))

    @app.post("/api/wsjtx/qso/enabled")
    def set_qso_enabled(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
        return qso_action("set_enabled", payload.get("enabled"))

    @app.post("/api/wsjtx/qso/auto")
    def set_qso_auto(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
        return qso_action("set_auto_sequence", payload.get("enabled"))

    @app.post("/api/wsjtx/qso/cq")
    def call_qso_cq(payload: dict[str, Any] = Body(default={})) -> dict[str, Any]:
        return qso_action("call_cq")

    @app.get("/api/wsjtx/qso/adif")
    def export_qso_adif() -> Response:
        if get_qso is None:
            raise HTTPException(status_code=503, detail="WSJT-X contact history is unavailable")
        try:
            exported = get_qso().contacts.export_adif()
        except RuntimeError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        return Response(exported, media_type="text/plain", headers={"Content-Disposition": 'attachment; filename="wsjtx-contacts.adi"'})

    @app.websocket("/api/wsjtx/events")
    async def wsjtx_events(websocket: WebSocket) -> None:
        await websocket.accept()
        manager = get_manager()
        try:
            subscriber = manager.subscribe()
        except RuntimeError:
            await websocket.close(code=1013)
            return
        try:
            await websocket.send_json({"type": "status", "status": await asyncio.to_thread(snapshot)})
            while True:
                try:
                    event = await asyncio.to_thread(subscriber.get, True, 1.0)
                except Empty:
                    event = {"type": "status", "status": await asyncio.to_thread(snapshot)}
                else:
                    if get_engine is not None or get_qso is not None:
                        current = await asyncio.to_thread(snapshot)
                        event = {**event, "status": current}
                await websocket.send_json(event)
        except WebSocketDisconnect:
            return
        finally:
            manager.unsubscribe(subscriber)
