from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import Body, FastAPI, HTTPException, Query


def register_tracking_api(
    app: FastAPI,
    *,
    get_sdr_manager: Callable[[], Any],
    get_tx_radio_manager: Callable[[], Any],
    get_rx_tracking_manager: Callable[[], Any],
    get_rotator_manager: Callable[[], Any],
    get_or_create_rx_tracking_manager: Callable[[int | None, int], Any],
    disabled_sdr_snapshot: Callable[[], Any],
    disabled_radio_snapshot: Callable[[], Any],
    disabled_rotator_snapshot: Callable[[], Any],
    payload_norad_id: Callable[[dict[str, Any]], int | None],
    payload_frequency_profile_index: Callable[[dict[str, Any]], int],
    build_orbital_engine: Callable[[], Any],
) -> None:
    @app.get("/api/devices/sdr/frequency")
    def get_sdr_frequency() -> dict[str, object]:
        sdr_manager = get_sdr_manager()
        if sdr_manager is None:
            return disabled_sdr_snapshot().to_dict()
        return sdr_manager.snapshot().to_dict()

    @app.post("/api/devices/sdr/frequency")
    def set_sdr_frequency(payload: dict[str, Any] = Body(...)) -> dict[str, object]:
        sdr_manager = get_sdr_manager()
        if sdr_manager is None:
            raise HTTPException(
                status_code=409,
                detail="RX control is off or not configured",
            )

        try:
            frequency_hz = int(payload["frequency_hz"])
        except (KeyError, TypeError, ValueError) as exc:
            raise HTTPException(
                status_code=400,
                detail="Request body must include integer frequency_hz",
            ) from exc

        try:
            return sdr_manager.set_frequency(frequency_hz).to_dict()
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc

    @app.get("/api/devices/tx/frequency")
    def get_tx_frequency() -> dict[str, object]:
        tx_radio_manager = get_tx_radio_manager()
        if tx_radio_manager is None:
            return disabled_radio_snapshot().to_dict()
        return tx_radio_manager.poll_once().to_dict()

    @app.get("/api/tracking/rx")
    def get_rx_tracking() -> dict[str, object]:
        rx_tracking_manager = get_rx_tracking_manager()
        if rx_tracking_manager is None:
            return {
                "active": False,
                "sync_offsets": True,
                "error": "RX tracking has not been started",
            }
        return rx_tracking_manager.snapshot().to_dict()

    @app.post("/api/tracking/rx/start")
    def start_rx_tracking(payload: dict[str, Any] | None = Body(default=None)) -> dict[str, object]:
        norad_id = None
        frequency_profile_index = 0
        if payload:
            raw_norad = payload.get("norad_id")
            norad_id = int(raw_norad) if raw_norad else None
            raw_profile_index = payload.get("frequency_profile_index")
            frequency_profile_index = int(raw_profile_index) if raw_profile_index else 0
        manager = get_or_create_rx_tracking_manager(norad_id, frequency_profile_index)
        if payload and "sync_offsets" in payload:
            manager.set_offset_sync(bool(payload["sync_offsets"]))
        manager.start()
        return manager.snapshot().to_dict()

    @app.post("/api/tracking/rx/stop")
    def stop_rx_tracking() -> dict[str, object]:
        rx_tracking_manager = get_rx_tracking_manager()
        if rx_tracking_manager is None:
            return {"active": False, "error": None}
        rx_tracking_manager.stop()
        return rx_tracking_manager.snapshot().to_dict()

    @app.post("/api/tracking/rx/reset-offset")
    def reset_rx_tracking_offset(payload: dict[str, Any] | None = Body(default=None)) -> dict[str, object]:
        payload = payload or {}
        manager = get_or_create_rx_tracking_manager(
            payload_norad_id(payload),
            payload_frequency_profile_index(payload),
        )
        if "sync_offsets" in payload:
            manager.set_offset_sync(bool(payload["sync_offsets"]))
        return manager.reset_offset().to_dict()

    @app.post("/api/tracking/offset-sync")
    def set_tracking_offset_sync(payload: dict[str, Any] = Body(...)) -> dict[str, object]:
        manager = get_or_create_rx_tracking_manager(
            payload_norad_id(payload),
            payload_frequency_profile_index(payload),
        )
        return manager.set_offset_sync(bool(payload.get("enabled", True))).to_dict()

    @app.post("/api/tracking/rx/step")
    def step_rx_tracking_offset(payload: dict[str, Any] = Body(...)) -> dict[str, object]:
        manager = get_or_create_rx_tracking_manager(
            payload_norad_id(payload),
            payload_frequency_profile_index(payload),
        )
        if "sync_offsets" in payload:
            manager.set_offset_sync(bool(payload["sync_offsets"]))
        try:
            step_hz = int(payload["step_hz"])
        except (KeyError, TypeError, ValueError) as exc:
            raise HTTPException(status_code=400, detail="Request body must include integer step_hz") from exc
        return manager.adjust_downlink_offset(step_hz).to_dict()

    @app.post("/api/tracking/tx/step")
    def step_tx_tracking_offset(payload: dict[str, Any] = Body(...)) -> dict[str, object]:
        manager = get_or_create_rx_tracking_manager(
            payload_norad_id(payload),
            payload_frequency_profile_index(payload),
        )
        if "sync_offsets" in payload:
            manager.set_offset_sync(bool(payload["sync_offsets"]))
        try:
            step_hz = int(payload["step_hz"])
        except (KeyError, TypeError, ValueError) as exc:
            raise HTTPException(status_code=400, detail="Request body must include integer step_hz") from exc
        return manager.adjust_uplink_offset(step_hz).to_dict()

    @app.get("/api/devices/rotator")
    def get_rotator_status() -> dict[str, object]:
        rotator_manager = get_rotator_manager()
        if rotator_manager is None:
            return disabled_rotator_snapshot().to_dict()
        return rotator_manager.poll_once().to_dict()

    @app.post("/api/devices/rotator/stop")
    def stop_rotator() -> dict[str, object]:
        rotator_manager = get_rotator_manager()
        if rotator_manager is None:
            raise HTTPException(
                status_code=409,
                detail="Rotator control is off or not configured",
            )
        return rotator_manager.stop().to_dict()

    @app.post("/api/devices/rotator/move")
    def move_rotator(payload: dict[str, Any] = Body(...)) -> dict[str, object]:
        rotator_manager = get_rotator_manager()
        if rotator_manager is None:
            raise HTTPException(
                status_code=409,
                detail="Rotator control is off or not configured",
            )
        try:
            azimuth_deg = float(payload["azimuth_deg"])
            elevation_deg = float(payload["elevation_deg"])
        except (KeyError, TypeError, ValueError) as exc:
            raise HTTPException(
                status_code=400,
                detail="Request body must include numeric azimuth_deg and elevation_deg",
            ) from exc
        if not (0.0 <= azimuth_deg <= 359.0):
            raise HTTPException(status_code=400, detail="Azimuth must be between 0 and 359")
        if not (0.0 <= elevation_deg <= 90.0):
            raise HTTPException(status_code=400, detail="Elevation must be between 0 and 90")
        snapshot = rotator_manager.manual_position(azimuth_deg, elevation_deg)
        if snapshot.pass_active:
            raise HTTPException(status_code=409, detail="PASS ACTIVE, manual mode disabled")
        return snapshot.to_dict()

    @app.post("/api/devices/rotator/home")
    def home_rotator() -> dict[str, object]:
        rotator_manager = get_rotator_manager()
        if rotator_manager is None:
            raise HTTPException(
                status_code=409,
                detail="Rotator control is off or not configured",
            )
        snapshot = rotator_manager.send_home()
        if snapshot.pass_active:
            raise HTTPException(status_code=409, detail="PASS ACTIVE, manual mode disabled")
        return snapshot.to_dict()

    @app.get("/api/tracking/ground-track")
    def get_tracking_ground_track(
        norad_id: int = Query(..., ge=1),
        minutes_before: int = Query(default=45, ge=0, le=240),
        minutes_after: int = Query(default=45, ge=1, le=240),
        step_seconds: int = Query(default=60, ge=5, le=300),
    ) -> dict[str, object]:
        try:
            engine = build_orbital_engine()
        except HTTPException:
            return {"norad_id": norad_id, "points": []}

        now_utc = datetime.now(timezone.utc)
        start_utc = now_utc - timedelta(minutes=minutes_before)
        end_utc = now_utc + timedelta(minutes=minutes_after)
        try:
            points = engine.get_ground_track(
                norad_id=norad_id,
                start_utc=start_utc,
                end_utc=end_utc,
                step_seconds=step_seconds,
            )
        except KeyError:
            points = []
        return {
            "norad_id": norad_id,
            "start_utc": start_utc.isoformat(),
            "end_utc": end_utc.isoformat(),
            "points": points,
        }
