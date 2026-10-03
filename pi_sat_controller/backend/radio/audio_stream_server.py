"""RX audio stream server for out-of-process listeners.

The server consumes Pi-Sat's native PCM fan-out. External DATA Decode selects
one receive channel and demodulates the radio's LAN IF before the existing TCP
framing; ordinary stream use still forwards selected PCM unchanged.

Each connected listener owns a queue and a writer thread. A listener that stops
reading therefore stalls only itself: the reader thread keeps draining the
radio's fan-out and keeps the other listeners current. Every queue is bounded
so an abandoned connection cannot grow without limit. The bound is deliberately
generous -- tens of seconds of audio -- because the stream is meant to be
lossless in normal operation, and overflow is reported rather than silent.
"""

from __future__ import annotations

import logging
import select
import socket
from collections import deque
from collections.abc import Callable
from threading import Event, Lock, Thread
from typing import TYPE_CHECKING, Any

from pi_sat_controller.backend.radio.audio_stream_protocol import (
    build_audio_frame,
    build_preamble,
    extract_pcm,
)

if TYPE_CHECKING:
    from pi_sat_controller.backend.radio.wide_fm import WideFmDemodulator

LOGGER = logging.getLogger(__name__)

# Enough audio that only a listener which has genuinely stopped reading can
# overflow it. At 48 kHz mono this is roughly ten seconds.
CLIENT_QUEUE_BYTES = 1 << 20
ACCEPT_POLL_S = 0.5
MAX_CLIENTS = 8


def radio_channels(rx_codec: str) -> int:
    """Receive channels Pi-Sat is decoding from the radio for this codec."""
    return 2 if rx_codec == "lpcm16_stereo" else 1


class _Client:
    """One listener: its own bounded queue and its own writer thread."""

    def __init__(
        self, conn: socket.socket, address: tuple[str, int], max_bytes: int,
        *, drop_oldest: bool = True,
    ) -> None:
        self.conn = conn
        self.address = address
        self.max_bytes = max_bytes
        self.drop_oldest = drop_oldest
        self._queue_lock = Lock()
        self.queue: deque[bytes] = deque()
        self.queued_bytes = 0
        self.preamble_pending = True
        self.overflow_dropped = 0
        self.frames_sent = 0
        self.stop = Event()
        self.wake = Event()

    def push(self, item: bytes) -> None:
        """Append one message, freeing the oldest audio if over the bound.

        Overflow is not a policy: a healthy listener never reaches the bound.
        Dropping the oldest audio keeps the newest audio moving so the stream
        recovers by itself once the listener reads again.
        """
        with self._queue_lock:
            if not self.drop_oldest and self.queued_bytes + len(item) > self.max_bytes:
                self.overflow_dropped += 1
                raise BufferError("External DATA Decode stream queue exceeded its buffer; audio stopped")
            self.queue.append(item)
            self.queued_bytes += len(item)
            if self.drop_oldest:
                while self.queued_bytes > self.max_bytes and len(self.queue) > 1:
                    self.queued_bytes -= len(self.queue.popleft())
                    self.overflow_dropped += 1
        self.wake.set()

    def reset(self) -> None:
        """Forget buffered audio and require a fresh preamble.

        Used when the radio's PCM format can change underneath an open
        connection, so a listener never splices samples from two formats.
        """
        with self._queue_lock:
            self.queue.clear()
            self.queued_bytes = 0
            self.preamble_pending = True

    def pop(self) -> bytes | None:
        with self._queue_lock:
            if not self.queue:
                return None
            item = self.queue.popleft()
            self.queued_bytes -= len(item)
            return item

    def wait(self, timeout: float) -> None:
        self.wake.wait(timeout)
        self.wake.clear()

    def close(self) -> None:
        self.stop.set()
        self.wake.set()
        try:
            self.conn.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            self.conn.close()
        except OSError:
            pass


class RxAudioStreamServer:
    """Serve framed SUB/RX mono PCM to any number of TCP listeners."""

    def __init__(
        self,
        *,
        host: str,
        port: int,
        channel: str,
        get_controller: Callable[[], Any],
        sample_rate: int = 16000,
        channels: int = 2,
        max_clients: int = MAX_CLIENTS,
        external_decode: bool = False,
    ) -> None:
        self.host = host
        self.port = port
        # External DATA Decode follows Pi-Sat's RX role (SUB), which is the
        # right sample in the IC-9700 stereo LAN stream.
        self.channel = "right" if external_decode else channel
        self._get_controller = get_controller
        self.sample_rate = int(sample_rate)
        self.channels = int(channels)
        self.max_clients = max(1, int(max_clients))
        self.external_decode = external_decode
        if external_decode and self.sample_rate != 48000:
            raise ValueError(f"External DATA Decode requires 48 kHz receive audio; configured rate is {self.sample_rate} Hz")
        self._stop = Event()
        self._wake = Event()
        self._clients_lock = Lock()
        self._clients: list[_Client] = []
        self._listener: socket.socket | None = None
        self._threads: list[Thread] = []
        self._sequence = 0
        self._accepted = 0
        self._refused = 0
        self._frames = 0
        self._audio_bytes = 0
        self._overflow_dropped = 0
        self._source_dropped = 0
        self._source_dropped_seen = 0
        self.running = False
        self.last_error: str | None = None
        self._decode_allowed = external_decode
        self._decode_active = False
        self._demodulator: WideFmDemodulator | None = None
        self._session_controller: Any = None
        self._session_generation: int | None = None
        self._previous_mode: tuple[str, int | None] | None = None
        self._had_client = False
        self._awaiting_client = False

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind((self.host, self.port))
        listener.listen(8)
        listener.settimeout(ACCEPT_POLL_S)
        self._listener = listener
        self.port = listener.getsockname()[1]
        self.running = True
        self._threads = [
            Thread(target=self._accept_loop, name="rx-audio-stream-accept", daemon=True),
            Thread(target=self._read_loop, name="rx-audio-stream-read", daemon=True),
        ]
        for thread in self._threads:
            thread.start()
        LOGGER.info(
            "RX audio stream listening on %s:%d (SUB/RX mono, %s sample)",
            self.host, self.port, self.channel,
        )

    def shutdown(self) -> None:
        if not self.running and self._listener is None:
            return
        self.running = False
        self._stop.set()
        self._wake.set()
        listener = self._listener
        self._listener = None
        if listener is not None:
            try:
                listener.close()
            except OSError:
                pass
        for client in self._client_snapshot():
            client.close()
        for thread in self._threads:
            thread.join(timeout=12.0)
        self._threads = []
        with self._clients_lock:
            self._clients = []
        LOGGER.info("RX audio stream stopped")

    def snapshot(self) -> dict[str, object]:
        clients = self._client_snapshot()
        return {
            "enabled": self._decode_allowed if self.external_decode else True,
            "running": self.running,
            "host": self.host,
            "port": self.port,
            "channel": self.channel,
            "listeners": len(clients),
            "max_clients": self.max_clients,
            "accepted": self._accepted,
            "refused": self._refused,
            "frames_sent": self._frames,
            "audio_bytes": self._audio_bytes,
            "overflow_dropped": self._overflow_dropped + sum(client.overflow_dropped for client in clients),
            "source_dropped": self._source_dropped,
            "last_error": self.last_error,
            "active": self._decode_active,
        }

    # -- internals ---------------------------------------------------------

    def _client_snapshot(self) -> list[_Client]:
        with self._clients_lock:
            return list(self._clients)

    def _close_clients(self) -> None:
        for client in self._client_snapshot():
            client.close()

    def _count_source_drops(self, audio_queue: Any) -> None:
        current = int(getattr(audio_queue, "dropped", 0))
        self._source_dropped += max(0, current - self._source_dropped_seen)
        self._source_dropped_seen = current

    def _add_client(self, client: _Client) -> None:
        client.push(build_preamble(self.sample_rate, self._output_channels()))
        with client._queue_lock:
            client.preamble_pending = False
        with self._clients_lock:
            self._clients.append(client)
        self._accepted += 1
        # Ask the reader to hand this listener its preamble immediately instead
        # of waiting for the next wake-up.
        self._wake.set()

    def _remove_client(self, client: _Client) -> None:
        with self._clients_lock:
            self._clients = [item for item in self._clients if item is not client]
            self._overflow_dropped += client.overflow_dropped
        client.close()

    def _accept_loop(self) -> None:
        while not self._stop.is_set():
            listener = self._listener
            if listener is None:
                return
            try:
                conn, address = listener.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            if len(self._client_snapshot()) >= self.max_clients:
                self._refused += 1
                LOGGER.warning(
                    "RX audio stream listener refused from %s:%d: client limit reached",
                    address[0], address[1],
                )
                try:
                    conn.close()
                except OSError:
                    pass
                continue
            client = _Client(conn, address, CLIENT_QUEUE_BYTES, drop_oldest=not self.external_decode)
            self._add_client(client)
            Thread(
                target=self._write_loop,
                args=(client,),
                name="rx-audio-stream-client",
                daemon=True,
            ).start()
            LOGGER.info("RX audio stream listener connected from %s:%d", address[0], address[1])

    def _read_loop(self) -> None:
        controller = None
        audio_queue = None
        try:
            while not self._stop.is_set():
                current = self._get_controller()
                if current is not controller:
                    self._end_decode_session()
                    if controller is not None and audio_queue is not None:
                        self._count_source_drops(audio_queue)
                        controller.unsubscribe_audio(audio_queue)
                    controller = current
                    audio_queue = None
                    self._source_dropped_seen = 0
                    # A different radio can mean a different PCM format, so no
                    # listener keeps audio across the change.
                    for client in self._client_snapshot():
                        client.reset()

                clients = self._client_snapshot()
                if self.external_decode:
                    if self._decode_active and (
                        controller is None
                        or not controller.try_snapshot().get("connected")
                        or controller.connection_generation() != self._session_generation
                    ):
                        self._end_decode_session()
                        self._decode_allowed = False
                        self.last_error = "Radio disconnected during External DATA Decode; enable it again"
                        self._close_clients()
                    if (self._decode_allowed and not self._decode_active and controller is not None
                            and (not self._awaiting_client or clients)):
                        if controller.try_snapshot().get("connected"):
                            try:
                                self._begin_decode_session(controller)
                                self._awaiting_client = False
                            except Exception as exc:
                                LOGGER.exception("External DATA Decode startup failed")
                                self.last_error = str(exc)
                                self._decode_allowed = False
                                self._end_decode_session()
                                self._close_clients()

                clients = self._client_snapshot()
                if self.external_decode and self._had_client and not clients:
                    self._end_decode_session()
                    self._had_client = False
                    self._awaiting_client = True
                if clients:
                    self._had_client = True
                if controller is None or not clients or (self.external_decode and not self._decode_active):
                    if controller is not None and audio_queue is not None:
                        self._count_source_drops(audio_queue)
                        controller.unsubscribe_audio(audio_queue)
                        audio_queue = None
                        self._source_dropped_seen = 0
                    self._wake.wait(ACCEPT_POLL_S)
                    self._wake.clear()
                    continue
                if audio_queue is None:
                    if self.external_decode:
                        audio_queue = controller.subscribe_audio(
                            self._wake.set, max_seconds=5.0, drop_oldest=False,
                        )
                    else:
                        audio_queue = controller.subscribe_audio(self._wake.set)
                    self._source_dropped_seen = 0

                packets = (
                    controller.read_audio(audio_queue)
                    if controller is not None and audio_queue is not None
                    else []
                )
                self._count_source_drops(audio_queue)
                if self.external_decode and audio_queue.overrun:
                    raise BufferError("External DATA Decode source buffer exceeded five seconds; audio stopped")
                self._dispatch(controller, self.channels, packets)
                if not packets:
                    self._wake.wait(ACCEPT_POLL_S)
                    self._wake.clear()
        except BufferError as exc:
            LOGGER.error("RX audio stream stopped: %s", exc)
            self.last_error = str(exc)
            self._decode_allowed = False
            self._close_clients()
        except Exception:
            LOGGER.exception("RX audio stream reader stopped unexpectedly")
            self.last_error = "reader stopped unexpectedly"
            self._decode_allowed = False
            self._close_clients()
        finally:
            if controller is not None and audio_queue is not None:
                try:
                    self._count_source_drops(audio_queue)
                    controller.unsubscribe_audio(audio_queue)
                except Exception:
                    LOGGER.debug("RX audio stream unsubscribe failed", exc_info=True)
            self._end_decode_session()

    def _begin_decode_session(self, controller: Any) -> None:
        from pi_sat_controller.backend.radio.wide_fm import WideFmDemodulator

        demodulator = WideFmDemodulator(sample_rate=self.sample_rate)
        side = "SUB"
        state = controller.snapshot()[side.lower()]
        mode = state.get("mode")
        if mode and state.get("data_mode"):
            mode += "-DATA"
        self._session_controller = controller
        self._session_generation = controller.connection_generation()
        self._previous_mode = (mode, state.get("filter")) if mode else None
        controller.set_mode(side, "FM-DATA")
        controller.set_lan_audio_output("IF")
        self._demodulator = demodulator
        self._decode_active = True
        self.last_error = None
        LOGGER.info("External DATA Decode active on IC-9700 %s", side)

    def _end_decode_session(self) -> None:
        controller = self._session_controller
        self._decode_active = False
        self._demodulator = None
        self._session_controller = None
        self._session_generation = None
        previous = self._previous_mode
        self._previous_mode = None
        if controller is None:
            return
        if not controller.try_snapshot().get("connected"):
            return
        # Restore AF first, even when restoring the operating mode fails.
        for attempt in range(2):
            try:
                controller.set_lan_audio_output("AF")
                break
            except Exception:
                LOGGER.exception("Could not restore IC-9700 LAN AF output (attempt %d)", attempt + 1)
                self.last_error = "Could not restore IC-9700 LAN AF output"
        if previous is not None:
            try:
                side = "SUB"
                current = controller.snapshot()[side.lower()]
                if current.get("mode") == "FM" and current.get("data_mode") and current.get("filter") == 1:
                    controller.set_mode(side, previous[0])
                    if previous[1] in (1, 2, 3):
                        controller.set_filter(side, previous[1])
            except Exception:
                LOGGER.exception("Could not restore IC-9700 receive mode")

    def _dispatch(self, controller: Any, channels: int, packets: list[bytes]) -> None:
        clients = self._client_snapshot()
        if not clients:
            return
        preamble = build_preamble(self.sample_rate, self._output_channels())
        for packet in packets:
            pcm, _output_channels = extract_pcm(packet, channels, self.channel)
            if not pcm:
                continue
            if self.external_decode:
                try:
                    import numpy as np

                    assert self._demodulator is not None
                    samples = np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32768.0
                    demodulated = self._demodulator.process(samples)
                    pcm = np.clip(np.rint(demodulated * 32768.0), -32768, 32767).astype("<i2").tobytes()
                    _output_channels = 1
                except Exception as exc:
                    LOGGER.exception("External DATA Decode demodulation failed")
                    self.last_error = str(exc)
                    self._decode_allowed = False
                    self._end_decode_session()
                    self._close_clients()
                    return
            # sample_count is audio frames, not individual interleaved samples.
            # Stereo therefore contributes two 16-bit values per frame.
            frame = build_audio_frame(
                self._sequence,
                len(pcm) // (2 * max(1, _output_channels)),
                pcm,
            )
            self._sequence = (self._sequence + 1) & 0xFFFFFFFF
            self._frames += 1
            self._audio_bytes += len(pcm)
            for client in clients:
                with client._queue_lock:
                    preamble_pending = client.preamble_pending
                    if preamble_pending:
                        client.preamble_pending = False
                if preamble_pending:
                    client.push(preamble)
                client.push(frame)
        # A listener accepted while the radio is silent still has to learn the
        # stream format, so the preamble is not tied to the first audio block.
        for client in clients:
            with client._queue_lock:
                preamble_pending = client.preamble_pending
                if preamble_pending:
                    client.preamble_pending = False
            if preamble_pending:
                client.push(preamble)

    def _output_channels(self) -> int:
        return 2 if self.channel == "both" and self.channels == 2 else 1

    def _write_loop(self, client: _Client) -> None:
        # A blocking send keeps frame boundaries intact; a listener that stalls
        # is bounded by its queue rather than by a partial frame. Shutdown
        # closes the socket, which unblocks this thread.
        try:
            while not self._stop.is_set() and not client.stop.is_set():
                try:
                    if select.select([client.conn], [], [], 0)[0]:
                        if not client.conn.recv(1):
                            return
                except OSError:
                    return
                item = client.pop()
                if item is None:
                    client.wait(ACCEPT_POLL_S)
                    continue
                try:
                    client.conn.sendall(item)
                except OSError:
                    return
                client.frames_sent += 1
        finally:
            self._remove_client(client)
            LOGGER.info(
                "RX audio stream listener %s:%d disconnected",
                client.address[0], client.address[1],
            )
