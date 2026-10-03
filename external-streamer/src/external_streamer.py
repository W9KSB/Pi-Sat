from __future__ import annotations

from collections import deque
import socket
import struct
import threading
import tkinter as tk
from tkinter import ttk

import sounddevice as sd


MAGIC = b"PISATAUD"
PROTOCOL_VERSION = 1
PREAMBLE = struct.Struct("<8sHHIBB6x")
FRAME = struct.Struct("<BBHII")
FRAME_TYPE_AUDIO = 1
MAX_FRAME_BYTES = 65535
BUFFER_SECONDS = 5.0
STARTUP_BUFFER_SECONDS = 0.25


def recv_exact(conn: socket.socket, size: int) -> bytes:
    chunks: list[bytes] = []
    remaining = size
    while remaining:
        chunk = conn.recv(remaining)
        if not chunk:
            raise ConnectionError("Pi-Sat closed the audio stream")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def parse_preamble(data: bytes) -> tuple[int, int]:
    magic, version, header_size, sample_rate, channels, sample_format = PREAMBLE.unpack(data)
    if magic != MAGIC or version != PROTOCOL_VERSION or header_size != PREAMBLE.size:
        raise ValueError("This is not a supported Pi-Sat audio stream")
    if sample_format != 1 or channels not in (1, 2) or sample_rate <= 0:
        raise ValueError("Pi-Sat stream is not signed 16-bit PCM")
    return sample_rate, channels


class PcmBuffer:
    def __init__(self, sample_rate: int, channels: int) -> None:
        self._bytes_per_second = sample_rate * 2 * channels
        self._limit = max(1, int(sample_rate * BUFFER_SECONDS * 2 * channels))
        self._chunks: deque[bytes] = deque()
        self._bytes = 0
        self._condition = threading.Condition()
        self.dropped_bytes = 0

    def put(self, payload: bytes) -> None:
        with self._condition:
            if self._bytes + len(payload) > self._limit:
                self.dropped_bytes += 1
                raise BufferError("Audio buffer exceeded 5 seconds; playback stopped before skipping PCM")
            self._chunks.append(payload)
            self._bytes += len(payload)
            self._condition.notify()

    def get(self, timeout: float = 0.5) -> bytes | None:
        with self._condition:
            if not self._chunks:
                self._condition.wait(timeout)
            if not self._chunks:
                return None
            payload = self._chunks.popleft()
            self._bytes -= len(payload)
            return payload

    def wait_for_startup(self, stop: threading.Event) -> bool:
        minimum = int(self._bytes_per_second * STARTUP_BUFFER_SECONDS)
        with self._condition:
            while self._bytes < minimum and not stop.is_set():
                self._condition.wait(0.1)
            return not stop.is_set()

    @property
    def queued_ms(self) -> int:
        with self._condition:
            return round(self._bytes * 1000 / self._bytes_per_second)


class StreamSession:
    def __init__(self, host: str, port: int, device: int | None, on_status) -> None:
        self.host = host
        self.port = port
        self.device = device
        self.on_status = on_status
        self.stop = threading.Event()
        self.connection: socket.socket | None = None
        self.connection_lock = threading.Lock()
        self.thread = threading.Thread(target=self._receive_loop, name="pi-sat-rx-receive", daemon=True)
        self.playback_thread: threading.Thread | None = None
        self.buffer: PcmBuffer | None = None
        self.sample_rate = 0
        self.frames = 0
        self.bytes = 0
        self.gaps = 0
        self.output_underflows = 0
        self.error = ""

    def start(self) -> None:
        self.thread.start()

    def close(self) -> None:
        self.stop.set()
        with self.connection_lock:
            conn = self.connection
            self.connection = None
        if conn is not None:
            try:
                conn.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                conn.close()
            except OSError:
                pass

    def _receive_loop(self) -> None:
        try:
            with socket.create_connection((self.host, self.port), timeout=8.0) as conn:
                # Audio can be silent for an arbitrary period. An idle TCP
                # connection is healthy; Stop closes the socket explicitly.
                conn.settimeout(None)
                with self.connection_lock:
                    self.connection = conn
                self.sample_rate, self.channels = parse_preamble(recv_exact(conn, PREAMBLE.size))
                self.buffer = PcmBuffer(self.sample_rate, self.channels)
                self.playback_thread = threading.Thread(target=self._playback_loop, name="pi-sat-rx-playback", daemon=True)
                self.playback_thread.start()
                expected: int | None = None
                self.on_status("connected")
                while not self.stop.is_set():
                    header = FRAME.unpack(recv_exact(conn, FRAME.size))
                    frame_type, _flags, sample_count, sequence, payload_bytes = header
                    if frame_type != FRAME_TYPE_AUDIO or payload_bytes > MAX_FRAME_BYTES:
                        raise ValueError("Invalid Pi-Sat audio frame")
                    payload = recv_exact(conn, payload_bytes)
                    if len(payload) != sample_count * 2 * self.channels:
                        raise ValueError("Pi-Sat audio frame sample count mismatch")
                    if expected is not None:
                        gap = (sequence - expected) & 0xFFFFFFFF
                        if gap:
                            self.gaps += gap
                    expected = (sequence + 1) & 0xFFFFFFFF
                    self.frames += 1
                    self.bytes += len(payload)
                    self.buffer.put(payload)
                    self.on_status("streaming")
        except (OSError, ConnectionError, ValueError, BufferError) as exc:
            if not self.stop.is_set():
                self.error = str(exc)
                self.on_status("error")
        finally:
            with self.connection_lock:
                self.connection = None
            self.stop.set()
            self.on_status("stopped")

    def _playback_loop(self) -> None:
        stream = None
        try:
            buffer = self.buffer
            if buffer is None or not buffer.wait_for_startup(self.stop):
                return
            stream = sd.RawOutputStream(
                samplerate=self.sample_rate,
                channels=self.channels,
                dtype="int16",
                device=self.device,
                latency=STARTUP_BUFFER_SECONDS,
            )
            stream.start()
            while not self.stop.is_set():
                payload = buffer.get()
                if payload:
                    if stream.write(payload):
                        self.output_underflows += 1
        except Exception as exc:
            if not self.stop.is_set():
                self.error = f"Audio output failed: {exc}"
                self.on_status("error")
                self.stop.set()
        finally:
            if stream is not None:
                stream.stop()
                stream.close()


class Application:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title("Pi-Sat RX Audio Stream")
        self.root.resizable(False, False)
        self.session: StreamSession | None = None
        self.host = tk.StringVar(value="127.0.0.1")
        self.port = tk.StringVar(value="8765")
        self.device = tk.StringVar()
        self.status = tk.StringVar(value="Stopped")
        self.stats = tk.StringVar(value="Frames 0 · Gaps 0 · 0 bytes")
        self._devices: list[tuple[str, int]] = []
        self._build_ui()
        self._refresh_devices()
        self.root.protocol("WM_DELETE_WINDOW", self._close)

    def _build_ui(self) -> None:
        frame = ttk.Frame(self.root, padding=14)
        frame.grid(sticky="nsew")
        ttk.Label(frame, text="Pi-Sat server").grid(row=0, column=0, sticky="w", padx=(0, 8), pady=4)
        ttk.Entry(frame, textvariable=self.host, width=24).grid(row=0, column=1, columnspan=2, sticky="ew", pady=4)
        ttk.Label(frame, text="Port").grid(row=1, column=0, sticky="w", padx=(0, 8), pady=4)
        ttk.Entry(frame, textvariable=self.port, width=10).grid(row=1, column=1, sticky="w", pady=4)
        ttk.Label(frame, text="Output device").grid(row=2, column=0, sticky="w", padx=(0, 8), pady=4)
        self.device_box = ttk.Combobox(frame, textvariable=self.device, state="readonly", width=42)
        self.device_box.grid(row=2, column=1, columnspan=2, sticky="ew", pady=4)
        ttk.Button(frame, text="Refresh devices", command=self._refresh_devices).grid(row=3, column=0, columnspan=3, sticky="ew", pady=(4, 10))
        self.start_button = tk.Button(frame, text="Start", bg="#1f8f3a", fg="white", activebackground="#2eaa4c", command=self._start)
        self.start_button.grid(row=4, column=0, sticky="ew", padx=(0, 5))
        self.stop_button = tk.Button(frame, text="Stop", bg="#b3261e", fg="white", activebackground="#d44238", state="disabled", command=self._stop)
        self.stop_button.grid(row=4, column=1, columnspan=2, sticky="ew")
        ttk.Label(frame, textvariable=self.status).grid(row=5, column=0, columnspan=3, sticky="w", pady=(12, 2))
        ttk.Label(frame, textvariable=self.stats).grid(row=6, column=0, columnspan=3, sticky="w")

    def _refresh_devices(self) -> None:
        try:
            devices = sd.query_devices()
            self._devices = [(str(item["name"]), index) for index, item in enumerate(devices) if item["max_output_channels"] > 0]
            self.device_box["values"] = [name for name, _ in self._devices]
            if self._devices and self.device.get() not in self.device_box["values"]:
                self.device.set(self._devices[0][0])
            self.status.set(f"{len(self._devices)} output device(s) available")
        except Exception as exc:
            self.status.set(f"Audio devices unavailable: {exc}")

    def _start(self) -> None:
        if self.session is not None:
            return
        try:
            host = self.host.get().strip()
            port = int(self.port.get().strip())
            if not host or not 1 <= port <= 65535:
                raise ValueError("Enter a valid server and port")
            selected = self.device.get()
            device = next((index for name, index in self._devices if name == selected), None)
            session: StreamSession
            session = StreamSession(
                host,
                port,
                device,
                lambda value: self._session_status(session, value),
            )
            self.session = session
            self.session.start()
            self.start_button.configure(state="disabled")
            self.stop_button.configure(state="normal")
            self.status.set("Connecting")
            self._poll_stats()
        except Exception as exc:
            self.status.set(str(exc))

    def _stop(self) -> None:
        if self.session is not None:
            self.session.close()
        self.session = None
        self.start_button.configure(state="normal")
        self.stop_button.configure(state="disabled")
        self.status.set("Stopped")

    def _session_status(self, session: StreamSession, value: str) -> None:
        self.root.after(0, lambda: self._apply_session_status(session, value))

    def _apply_session_status(self, session: StreamSession, value: str) -> None:
        if self.session is session:
            self.status.set(value.capitalize())

    def _poll_stats(self) -> None:
        session = self.session
        if session is None:
            return
        dropped = session.buffer.dropped_bytes if session.buffer is not None else 0
        queued_ms = session.buffer.queued_ms if session.buffer is not None else 0
        self.stats.set(
            f"Frames {session.frames:,} · Gaps {session.gaps:,} · {session.bytes:,} bytes"
            f" · Buffer drops {dropped:,} · Queued {queued_ms} ms"
            f" · Output underflows {session.output_underflows:,}"
        )
        if session.stop.is_set() and self.session is session:
            self.start_button.configure(state="normal")
            self.stop_button.configure(state="disabled")
            self.session = None
            if session.error:
                self.status.set(session.error)
        else:
            self.root.after(500, self._poll_stats)

    def _close(self) -> None:
        self._stop()
        self.root.destroy()


def main() -> None:
    root = tk.Tk()
    Application(root)
    root.mainloop()


if __name__ == "__main__":
    main()
