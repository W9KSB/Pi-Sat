from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from typing import Any

from fastapi import Body, FastAPI, HTTPException, WebSocket, WebSocketDisconnect

from pi_sat_controller.backend.radio.audio_websocket import stream_audio, stream_microphone
from pi_sat_controller.backend.radio.state_websocket import stream_radio_state

LOGGER = logging.getLogger(__name__)


def register_radio_api(
    app: FastAPI,
    *,
    get_controller: Callable[[], Any],
) -> None:
    # Count microphone uplinks so closing an RX listener cannot unkey a
    # transmission another session is feeding.
    microphone_sessions = 0

    @app.get("/api/radio")
    def get_radio() -> dict[str, object]:
        controller = get_controller()
        if controller is None:
            return {"enabled": False, "connected": False, "error": "Advanced Icom control is disabled."}
        return {"enabled": True, **controller.snapshot()}

    @app.post("/api/radio/connect")
    def connect_radio() -> dict[str, object]:
        controller = get_controller()
        if controller is None:
            raise HTTPException(status_code=409, detail="Advanced Icom control is disabled.")
        try:
            return {"ok": True, "enabled": True, **controller.start()}
        except Exception as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc

    @app.post("/api/radio/disconnect")
    def disconnect_radio() -> dict[str, object]:
        controller = get_controller()
        if controller is not None:
            try:
                controller.disconnect()
            except Exception as exc:
                raise HTTPException(status_code=502, detail=str(exc)) from exc
        return {"ok": True, "connected": False, "connecting": False}

    @app.post("/api/radio/select")
    def select_radio(payload: dict[str, Any] = Body(...)) -> dict[str, object]:
        controller = get_controller()
        if controller is None:
            raise HTTPException(status_code=409, detail="Advanced Icom control is disabled.")
        try:
            return {"ok": True, **controller.select(payload["physical_side"], payload["vfo"])}
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/radio/frequency")
    def set_frequency(payload: dict[str, Any] = Body(...)) -> dict[str, object]:
        controller = get_controller()
        if controller is None:
            raise HTTPException(status_code=409, detail="Advanced Icom control is disabled.")
        try:
            return {"ok": True, **controller.set_frequency(payload["physical_side"], payload.get("vfo"), payload["frequency_hz"])}
        except (KeyError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc

    @app.post("/api/radio/mode")
    def set_mode(payload: dict[str, Any] = Body(...)) -> dict[str, object]:
        controller = get_controller()
        if controller is None:
            raise HTTPException(status_code=409, detail="Advanced Icom control is disabled.")
        try:
            return {"ok": True, **controller.set_mode(payload["physical_side"], str(payload["mode"]), payload.get("vfo"))}
        except (KeyError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc

    @app.post("/api/radio/ptt")
    def set_ptt(payload: dict[str, Any] = Body(...)) -> dict[str, object]:
        controller = get_controller()
        if controller is None:
            raise HTTPException(status_code=409, detail="Advanced Icom control is disabled.")
        try:
            return {"ok": True, **controller.set_ptt(payload.get("enabled", False))}
        except Exception as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc

    @app.post("/api/radio/filter")
    def set_filter(payload: dict[str, Any] = Body(...)) -> dict[str, object]:
        controller = get_controller()
        if controller is None:
            raise HTTPException(status_code=409, detail="Advanced Icom control is disabled.")
        try:
            return {"ok": True, **controller.set_filter(payload["physical_side"], payload["filter"])}
        except (KeyError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc

    @app.post("/api/radio/scope")
    def configure_scope(payload: dict[str, Any] = Body(...)) -> dict[str, object]:
        controller = get_controller()
        if controller is None:
            raise HTTPException(status_code=409, detail="Advanced Icom control is disabled.")
        try:
            return {"ok": True, **controller.scope_configure(payload["physical_side"], payload.get("enabled", True), payload.get("span_hz"))}
        except (KeyError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc

    @app.post("/api/radio/level")
    def set_level(payload: dict[str, Any] = Body(...)) -> dict[str, object]:
        controller = get_controller()
        if controller is None:
            raise HTTPException(status_code=409, detail="Advanced Icom control is disabled.")
        try:
            return {"ok": True, **controller.set_level(payload["physical_side"], payload["control"], payload["value"])}
        except (KeyError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc

    @app.post("/api/radio/tune")
    def tune_radio(payload: dict[str, Any] = Body(...)) -> dict[str, object]:
        controller = get_controller()
        if controller is None:
            raise HTTPException(status_code=409, detail="Advanced Icom control is disabled.")
        try:
            return {"ok": True, **controller.tune(payload["physical_side"], payload["delta_hz"], payload.get("expected_frequency_hz"))}
        except (KeyError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc

    @app.post("/api/radio/equalize")
    def equalize_vfos(payload: dict[str, Any] = Body(...)) -> dict[str, object]:
        controller = get_controller()
        if controller is None:
            raise HTTPException(status_code=409, detail="Advanced Icom control is disabled.")
        try:
            return {"ok": True, **controller.equalize_vfos(payload["physical_side"])}
        except (KeyError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc

    @app.post("/api/radio/sub-rit")
    def set_sub_rit(payload: dict[str, Any] = Body(...)) -> dict[str, object]:
        controller = get_controller()
        if controller is None:
            raise HTTPException(status_code=409, detail="Advanced Icom control is disabled.")
        try:
            return {"ok": True, **controller.set_sub_rit(payload.get("offset_hz"), payload.get("enabled"))}
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc

    @app.post("/api/radio/microphone-config")
    def configure_microphone(payload: dict[str, Any] = Body(...)) -> dict[str, object]:
        controller = get_controller()
        if controller is None:
            raise HTTPException(status_code=409, detail="Advanced Icom control is disabled.")
        try:
            return {"ok": True, **controller.configure_microphone(
                payload.get("use_lan", False),
                payload.get("lan_mod_level"),
                payload.get("use_transport", False),
                payload.get("source"),
            )}
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc

    @app.post("/api/radio/dualwatch")
    def set_dualwatch(payload: dict[str, Any] = Body(...)) -> dict[str, object]:
        controller = get_controller()
        if controller is None:
            raise HTTPException(status_code=409, detail="Advanced Icom control is disabled.")
        try:
            return {"ok": True, **controller.set_dualwatch(payload["enabled"])}
        except (KeyError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc

    @app.websocket("/api/radio/audio")
    async def radio_audio(websocket: WebSocket) -> None:
        await websocket.accept()
        controller = get_controller()
        if controller is None:
            await websocket.close(code=1011, reason="Advanced Icom control is disabled")
            return
        try:
            await stream_audio(websocket, controller, get_controller,
                               holds_microphone=lambda: microphone_sessions > 0)
            await websocket.close(code=1000, reason="Radio configuration changed")
        except WebSocketDisconnect:
            return
        except Exception as exc:
            # Log RX socket failures so operators can diagnose lost listening audio.
            LOGGER.warning("RX audio session ended: %s", exc)
            await websocket.close(code=1011)

    @app.websocket("/api/radio/mic")
    async def radio_mic(websocket: WebSocket) -> None:
        """Browser microphone uplink, kept off the RX audio socket."""
        nonlocal microphone_sessions
        await websocket.accept()
        controller = get_controller()
        if controller is None:
            await websocket.close(code=1011, reason="Advanced Icom control is disabled")
            return
        microphone_sessions += 1
        try:
            await stream_microphone(websocket, controller, get_controller)
            await websocket.close(code=1000, reason="Radio configuration changed")
        except WebSocketDisconnect:
            return
        except Exception as exc:
            LOGGER.warning("Microphone session ended: %s", exc)
            await websocket.close(code=1011)
        finally:
            microphone_sessions -= 1

    @app.websocket("/api/radio/state")
    async def radio_state(websocket: WebSocket) -> None:
        """Push cached authoritative state; this never issues radio queries."""
        await websocket.accept()
        try:
            await stream_radio_state(websocket, get_controller)
        except WebSocketDisconnect:
            return
        except asyncio.TimeoutError:
            await websocket.close(code=1011, reason="Radio display client is too slow")

    @app.get("/api/radio/scope-data")
    def scope_data() -> dict[str, object]:
        controller = get_controller()
        if controller is None:
            return {"packets": []}
        return {"packets": [packet.hex() for packet in controller.drain_scope_packets()]}
