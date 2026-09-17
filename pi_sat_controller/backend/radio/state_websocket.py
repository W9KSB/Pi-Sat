"""Latest-frame scope delivery independent of slow, serialized CAT readbacks."""
from __future__ import annotations

import asyncio


# Packet counters change on every audio and scope frame. They are diagnostics,
# not operator state: comparing them re-sent the whole console snapshot ten
# times per second, forcing a full browser re-render that competed with audio
# delivery and waterfall painting. Real state changes still send immediately,
# and the counters ride along on those sends and the periodic heartbeat.
_VOLATILE_STATE_KEYS = frozenset({"rx_audio_packets", "tx_audio_packets", "scope_lines"})

# Scope lines are sampled at the display rate so a radio sweep is not aliased
# away by a slower polling loop. Delivery is still latest-frame-only.
_SCOPE_POLL_INTERVAL_S = 1 / 60


async def stream_radio_state(websocket, get_controller) -> None:
    async def send_updates():
        previous_controller = previous_state = previous_scope = None
        last_state_attempt = last_sent = 0.0
        loop = asyncio.get_running_loop()
        while True:
            controller = get_controller()
            now = loop.time()
            if controller is not previous_controller:
                previous_controller = controller
                previous_state = previous_scope = None
                last_state_attempt = 0.0
            payload = {}
            if now - last_state_attempt >= 0.1:
                last_state_attempt = now
                state = ({"enabled": False, "connected": False} if controller is None
                         else controller.try_snapshot())
                if state is not None:
                    if controller is not None:
                        state = {"enabled": True, **state}
                    comparison = {key: value for key, value in state.items() if key not in _VOLATILE_STATE_KEYS}
                    if comparison != previous_state or now - last_sent >= 2.0:
                        payload.update(state)
                        previous_state = comparison
                        last_sent = now
            if controller is not None:
                scope = controller.scope_snapshot()
                if scope != previous_scope:
                    payload["scope"] = scope
                    previous_scope = scope
            if payload:
                # Never accumulate a per-client spectrum backlog. A stalled
                # connection is closed by the API and reopens at the latest frame.
                await asyncio.wait_for(websocket.send_json(payload), timeout=0.25)
            await asyncio.sleep(_SCOPE_POLL_INTERVAL_S)

    async def receive_disconnect():
        while True:
            await websocket.receive_text()

    tasks = [asyncio.create_task(send_updates()), asyncio.create_task(receive_disconnect())]
    try:
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            task.result()
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
