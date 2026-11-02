"""Keep microphone backpressure and failures isolated from RX audio delivery."""
import asyncio
import json
import logging
from contextlib import suppress
import secrets
from threading import Event, Lock, Thread
from time import monotonic

from pi_sat_controller.backend.radio.mic_transport import (
    LegacyMicFrame,
    MIC_MAX_AGE_S,
    MicrophoneDiagnostics,
    MicrophoneMailbox,
    MicrophoneSessionValidator,
    MalformedMicFrame,
    parse_mic_message,
)

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


async def stream_microphone(
    websocket,
    controller,
    get_controller,
    *,
    debug_enabled=None,
) -> None:
    """Receive browser microphone PCM on a socket of its own.

    One frame that cannot reach the radio must never end this session: the
    operator would lose the microphone until the next reconnect, and the cause is
    usually a transient link state. The first failure of each run is reported and
    the pump keeps draining, so a recovered link recovers quietly. RX audio is
    unaffected either way because it travels on another connection.
    """
    transport_id = secrets.randbits(32) or 1
    sample_rate = int(controller.config.sample_rate)
    validator = MicrophoneSessionValidator(
        transport_id=transport_id,
        sample_rate=sample_rate,
    )
    enabled = debug_enabled or (lambda: False)
    diagnostics = MicrophoneDiagnostics(debug_enabled=enabled)
    reported_errors: set[str] = set()
    reported_lock = Lock()

    def transport_metrics() -> dict:
        snapshot = controller.try_snapshot()
        metrics = dict(snapshot.get("transport_diagnostics", {}))
        for key in (
            "tx_audio_udp_spacing_min_ms",
            "tx_audio_udp_spacing_max_ms",
        ):
            if key in snapshot:
                metrics[key] = snapshot[key]
        return metrics

    def report_worker_error(exc: Exception) -> None:
        error_type = type(exc).__name__
        with reported_lock:
            first = error_type not in reported_errors
            reported_errors.add(error_type)
        if first:
            LOGGER.warning("Microphone audio is not reaching the radio: %s", exc)

    mailbox = MicrophoneMailbox(
        controller=controller,
        diagnostics=diagnostics,
        on_error=report_worker_error,
        is_current=lambda: controller is get_controller(),
    )
    try:
        await websocket.send_json({
            "type": "mic_ready",
            "transport_id": transport_id,
            "sample_rate": sample_rate,
        })
        mailbox.start()
        while controller is get_controller():
            incoming = await websocket.receive()
            if incoming["type"] == "websocket.disconnect":
                break
            text = incoming.get("text")
            if text is not None:
                try:
                    message = json.loads(text)
                except (TypeError, ValueError):
                    continue
                if not isinstance(message, dict):
                    continue
                if message.get("type") == "mic_sync":
                    t1 = monotonic()
                    await websocket.send_json({
                        "type": "mic_sync",
                        "id": message.get("id"),
                        # The browser handshake clock uses milliseconds.
                        "t1": t1 * 1000.0,
                        "t2": monotonic() * 1000.0,
                    })
                elif message.get("type") == "mic_diagnostics":
                    counters = message.get("counters")
                    if isinstance(counters, dict):
                        diagnostics.update_client(counters)
                        diagnostics.maybe_emit(
                            monotonic(),
                            transport_diagnostics=transport_metrics,
                        )
                continue
            payload = incoming.get("bytes")
            if payload is None:
                continue
            received_at = monotonic()
            try:
                frame = parse_mic_message(payload)
            except MalformedMicFrame:
                diagnostics.drop("malformed")
                continue
            if isinstance(frame, LegacyMicFrame):
                if (
                    len(frame.pcm) < 2
                    or len(frame.pcm) % 2
                    or len(frame.pcm) > round(sample_rate * MIC_MAX_AGE_S) * 2
                ):
                    diagnostics.drop("frame_size")
                    continue
                frame = LegacyMicFrame(
                    pcm=frame.pcm,
                    received_at=received_at,
                    tx_generation=controller.transmit_generation,
                )
                diagnostics.legacy()
            else:
                validation = validator.validate(frame, received_at=received_at)
                if isinstance(validation, str):
                    diagnostics.drop(validation)
                    continue
                age_s, gap_s, sequence_gap = validation
                if sequence_gap:
                    diagnostics.gap()
                diagnostics.received(
                    frame,
                    received_at=received_at,
                    age_s=age_s,
                    gap_s=gap_s,
                )
            mailbox.submit(frame)
            diagnostics.maybe_emit(
                monotonic(),
                transport_diagnostics=transport_metrics,
            )
    finally:
        cleanup_done = Event()

        def cleanup() -> None:
            # Keep stop and release in one thread so cancellation cannot land
            # between them and skip the safety-critical unkey.
            try:
                mailbox.stop()
                if controller is get_controller():
                    # Only the socket that supplied the active transmission may
                    # release it; an old reconnect cannot unkey its replacement.
                    controller.release_ptt_if_audio_source_lost(transport_id)
            finally:
                cleanup_done.set()

        Thread(
            target=cleanup,
            name="icom-microphone-cleanup",
            daemon=True,
        ).start()
        deadline = monotonic() + 2.0
        while not cleanup_done.is_set() and monotonic() < deadline:
            try:
                # Keep yielding to RX and other WebSocket work while a blocked
                # controller release finishes on its owned cleanup thread.
                await asyncio.sleep(0.01)
            except asyncio.CancelledError:
                # Repeated cancellation cannot stop the owned cleanup thread;
                # continue only within the bounded teardown window.
                continue
