"""Keep microphone backpressure and failures isolated from RX audio delivery."""
import asyncio
import logging
from contextlib import suppress
from time import monotonic

LOGGER = logging.getLogger(__name__)


async def stream_audio(websocket, controller, get_controller, *, holds_microphone=None) -> None:
    """Send RX PCM to one listener until the client or the session ends.

    ``holds_microphone`` reports whether another session is still feeding the
    radio's transmitter. This socket can end on its own while the browser stays
    connected and keyed, so it must not unkey a transmission that something else
    is still feeding.
    """
    loop = asyncio.get_running_loop()
    available = asyncio.Event()

    def notify() -> None:
        # A final UDP packet can race event-loop shutdown/unsubscription.
        with suppress(RuntimeError):
            loop.call_soon_threadsafe(available.set)

    queue = controller.subscribe_audio(notify)

    async def send_rx() -> None:
        while controller is get_controller():
            available.clear()
            packets = controller.read_audio(queue)
            if not packets:
                try:
                    await asyncio.wait_for(available.wait(), timeout=1.0)
                except asyncio.TimeoutError:
                    pass
                continue
            deadline = monotonic() + 0.12
            for packet in packets:
                # Never keep feeding stale PCM into a backpressured connection.
                remaining = deadline - monotonic()
                if remaining <= 0:
                    raise TimeoutError("Audio client is not keeping up")
                await asyncio.wait_for(websocket.send_bytes(packet), timeout=remaining)

    async def watch_client() -> None:
        # This socket carries no uplink, so reading it exists only to notice a
        # close, reload or navigation immediately. Anything a client sends here
        # is ignored rather than treated as microphone audio.
        while controller is get_controller():
            message = await websocket.receive()
            if message["type"] == "websocket.disconnect":
                return

    tasks = [asyncio.create_task(send_rx()), asyncio.create_task(watch_client())]
    try:
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            task.result()
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        controller.unsubscribe_audio(queue)
        if controller is get_controller():
            # If Pi-Sat still holds the key and no live microphone session
            # remains, nothing can feed the transmitter, so command a release
            # instead of leaving the radio transmitting a bare carrier.
            microphone_alive = holds_microphone is not None and holds_microphone()
            if not microphone_alive:
                await asyncio.to_thread(controller.release_ptt_if_audio_source_lost)


async def stream_microphone(websocket, controller, get_controller) -> None:
    """Receive browser microphone PCM on a socket of its own.

    One frame that cannot reach the radio must never end this session: the
    operator would lose the microphone until the next reconnect, and the cause is
    usually a transient link state. The first failure of each run is reported and
    the pump keeps draining, so a recovered link recovers quietly. RX audio is
    unaffected either way because it travels on another connection.
    """
    reported = False
    try:
        while controller is get_controller():
            incoming = await websocket.receive_bytes()
            try:
                # The microphone path is gated by the controller's own transmit
                # state and never waits on the CI-V controller lock, so browser
                # audio keeps flowing while tracking work is in progress.
                await asyncio.to_thread(controller.send_microphone_audio, incoming)
            except Exception as exc:
                if not reported:
                    reported = True
                    LOGGER.warning("Microphone audio is not reaching the radio: %s", exc)
            else:
                reported = False
    finally:
        # This socket is the only microphone path into the radio, so its end must
        # release a key that only Pi-Sat could have been feeding.
        if controller is get_controller():
            await asyncio.to_thread(controller.release_ptt_if_audio_source_lost)
