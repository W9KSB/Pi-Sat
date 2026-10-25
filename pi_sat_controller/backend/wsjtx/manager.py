from __future__ import annotations

import copy
import hashlib
import ipaddress
import math
import socket
import threading
import time
from collections import OrderedDict, deque
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from queue import Empty, Full, Queue
from typing import Any

from .protocol import MAX_PACKET_BYTES, ProtocolError, parse_datagram

_MAX_CLIENTS = 32
_MAX_SUBSCRIBERS = 32


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class WsjtxManager:
    """Collect WSJT-X datagrams and address known local engine clients."""

    def __init__(self, *, get_settings: Callable[[], dict[str, Any]]) -> None:
        self._get_settings = get_settings
        self._lock = threading.RLock()
        self._lifecycle_lock = threading.Lock()
        self._socket: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._enabled = False
        self._running = False
        self._detail = ""
        self._host = "127.0.0.1"
        self._port = 2237
        self._timeout = 45.0
        self._max_decodes = 300
        self._clients: OrderedDict[str, dict[str, Any]] = OrderedDict()
        self._decodes: deque[dict[str, Any]] = deque()
        self._decode_keys: set[str] = set()
        self._subscribers: set[Queue] = set()
        self._sequence = 0
        self._packet_count = 0
        self._malformed_packets = 0
        try:
            self._load_settings()
        except Exception:
            # Invalid optional settings must not interrupt application startup.
            self._detail = "Invalid WSJT-X listener settings"

    def _load_settings(self) -> None:
        settings = self._get_settings()
        host = settings.get("udp_host", "127.0.0.1")
        port = settings.get("udp_port", 2237)
        timeout = settings.get("client_timeout_seconds", 45)
        maximum = settings.get("max_decodes", 300)
        if not isinstance(host, str) or not host.strip():
            raise ValueError("UDP host must be a nonempty address")
        if type(port) is not int or not 0 <= port <= 65535:
            raise ValueError("UDP port must be an integer from 0 to 65535")
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or not 0 < timeout <= 3600:
            raise ValueError("Client timeout must be between 0 and 3600 seconds")
        if type(maximum) is not int or not 1 <= maximum <= 5000:
            raise ValueError("Decode history limit must be between 1 and 5000")
        self._host, self._port = host.strip(), port
        self._timeout, self._max_decodes = float(timeout), maximum
        while len(self._decodes) > maximum:
            self._decode_keys.discard(self._decodes.pop()["key"])

    def _snapshot_locked(self) -> dict[str, Any]:
        connected = any(client["connected"] for client in self._clients.values())
        state = "disabled" if not self._enabled else "error" if self._detail else "connected" if self._running and connected else "listening"
        return {
            "enabled": self._enabled, "running": self._running,
            "state": state, "detail": self._detail,
            "udp_host": self._host, "udp_port": self._port,
            "packet_count": self._packet_count, "malformed_packets": self._malformed_packets,
            "clients": [copy.deepcopy({k: v for k, v in client.items() if not k.startswith("_")}) for client in self._clients.values()],
            "decodes": copy.deepcopy(list(self._decodes)), "sequence": self._sequence,
        }

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            if self._expire_locked(time.monotonic()):
                self._publish_locked()
            return self._snapshot_locked()

    def client_candidates(self) -> list[dict[str, Any]]:
        """Return engine metadata without copying the decode history."""
        with self._lock:
            self._expire_locked(time.monotonic())
            return [copy.deepcopy({key: value for key, value in client.items() if not key.startswith("_")})
                    for client in self._clients.values()]

    def client_snapshot(self, key: str) -> dict[str, Any] | None:
        with self._lock:
            self._expire_locked(time.monotonic())
            client = self._clients.get(key)
            return copy.deepcopy({field: value for field, value in client.items() if not field.startswith("_")}) if client else None

    def send_command(self, client_key: str, datagram: bytes) -> bool:
        """Send only to a connected loopback client learned by this listener."""
        if not isinstance(datagram, bytes) or not 12 <= len(datagram) <= MAX_PACKET_BYTES:
            raise ValueError("Invalid WSJT-X command datagram")
        with self._lock:
            self._expire_locked(time.monotonic())
            client = self._clients.get(client_key)
            if not self._running or self._socket is None or not client or not client["connected"]:
                return False
            try:
                if not ipaddress.ip_address(client["host"]).is_loopback:
                    return False
                return self._socket.sendto(datagram, (client["host"], client["port"])) == len(datagram)
            except (OSError, ValueError):
                return False

    def _publish_locked(self) -> None:
        self._sequence += 1
        if not self._subscribers:
            return
        event = {"type": "status", "status": self._snapshot_locked()}
        for subscriber in self._subscribers:
            try:
                subscriber.put_nowait(event)
            except Full:
                try:
                    subscriber.get_nowait()
                except Empty:
                    pass
                try:
                    subscriber.put_nowait(event)
                except Full:
                    pass

    def subscribe(self) -> Queue:
        with self._lock:
            if len(self._subscribers) >= _MAX_SUBSCRIBERS:
                raise RuntimeError("Too many WSJT-X event subscribers")
            subscriber: Queue = Queue(maxsize=2)
            self._subscribers.add(subscriber)
            return subscriber

    def unsubscribe(self, subscriber: Queue) -> None:
        with self._lock:
            self._subscribers.discard(subscriber)

    def set_enabled(self, enabled: bool) -> dict[str, Any]:
        if type(enabled) is not bool:
            raise ValueError("enabled must be a boolean")
        with self._lifecycle_lock:
            with self._lock:
                if enabled and self._running:
                    return self._snapshot_locked()
                old_socket, old_thread = self._socket, self._thread
                self._socket, self._thread = None, None
                self._enabled, self._running, self._detail = enabled, False, ""
                for client in self._clients.values():
                    client["connected"] = False
            stopped = True
            if old_thread is not None:
                old_thread.join(timeout=1.5)
                stopped = not old_thread.is_alive()
            if stopped and old_socket is not None:
                old_socket.close()
            if not stopped:
                with self._lock:
                    self._enabled = False
                    self._detail = "Previous WSJT-X UDP listener did not stop within 1.5 seconds"
                    self._publish_locked()
                return self.snapshot()
            with self._lock:
                if enabled:
                    listener: socket.socket | None = None
                    try:
                        self._load_settings()
                        listener = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                        listener.settimeout(0.5)
                        listener.bind((self._host, self._port))
                        self._port = listener.getsockname()[1]
                        self._socket = listener
                        self._running = True
                        self._thread = threading.Thread(target=self._receive, args=(listener,), name="wsjtx-rx", daemon=True)
                        self._thread.start()
                    except Exception as exc:
                        if listener is not None:
                            listener.close()
                        self._socket = None
                        self._thread = None
                        self._running = False
                        self._detail = f"Could not start WSJT-X UDP listener: {exc}"
                self._publish_locked()
                return self._snapshot_locked()

    def shutdown(self) -> None:
        self.set_enabled(False)

    def _expire_locked(self, now: float) -> bool:
        changed = False
        for client in self._clients.values():
            if client["connected"] and now - client["_last_seen"] >= self._timeout:
                client["connected"] = False
                changed = True
        return changed

    def _receive(self, listener: socket.socket) -> None:
        try:
            while True:
                with self._lock:
                    if self._socket is not listener:
                        return
                try:
                    data, address = listener.recvfrom(MAX_PACKET_BYTES + 1)
                except socket.timeout:
                    with self._lock:
                        if self._socket is listener and self._expire_locked(time.monotonic()):
                            self._publish_locked()
                    continue
                except OSError as exc:
                    with self._lock:
                        if self._socket is listener:
                            self._socket = None
                            self._running = False
                            self._detail = f"WSJT-X UDP listener stopped: {exc}"
                            for client in self._clients.values():
                                client["connected"] = False
                            self._publish_locked()
                    return
                with self._lock:
                    if self._socket is not listener:
                        return
                    self._handle_datagram_locked(data, address)
        finally:
            listener.close()

    def _clear_client_decodes_locked(self, key: str) -> None:
        self._decodes = deque(decode for decode in self._decodes if decode["client_key"] != key)
        self._decode_keys = {decode["key"] for decode in self._decodes}

    def _handle_datagram_locked(self, data: bytes, address: tuple[str, int]) -> None:
        self._packet_count += 1
        try:
            message = parse_datagram(data)
        except ProtocolError:
            self._malformed_packets += 1
            self._publish_locked()
            return
        now = _utc_now()
        self._expire_locked(time.monotonic())
        if message is None:
            self._publish_locked()
            return
        key = hashlib.sha256(f"{address[0]}:{address[1]}:{message['id']}".encode()).hexdigest()[:24]
        client = self._clients.get(key)
        if client is None:
            if len(self._clients) >= _MAX_CLIENTS:
                evicted, _ = self._clients.popitem(last=False)
                self._clear_client_decodes_locked(evicted)
            client = {"key": key, "id": message["id"], "host": address[0], "port": address[1], "connected": False, "version": "", "revision": "", "status": {}}
            self._clients[key] = client
        self._clients.move_to_end(key)
        if not client["connected"]:
            client["status"] = {}
            client["first_seen_utc"] = now.isoformat()
        client.update(connected=message["type"] != 6, last_seen_utc=now.isoformat(), schema=message["schema"], _last_seen=time.monotonic())
        kind = message["type"]
        if kind == 0:
            client.update(version=message["version"], revision=message["revision"])
        elif kind == 1:
            client["status"] = message["status"]
            client["status_sequence"] = self._sequence + 1
        elif kind == 3:
            self._clear_client_decodes_locked(key)
        elif kind == 2 and message["mode"] in ("FT8", "FT4"):
            # QTime has no date. The nearest UTC day preserves midnight decodes.
            decoded_time = now.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(milliseconds=message["time_ms"])
            if decoded_time - now > timedelta(hours=12):
                decoded_time -= timedelta(days=1)
            elif now - decoded_time > timedelta(hours=12):
                decoded_time += timedelta(days=1)
            decode = {field: message[field] for field in ("time_ms", "snr_db", "dt_seconds", "frequency_hz", "mode", "message", "low_confidence", "off_air")}
            decode.update(client_key=key, client_id=message["id"], time_utc=decoded_time.isoformat(timespec="milliseconds"))
            identity = repr(tuple(decode.items())).encode("utf-8")
            decode_key = hashlib.sha256(identity).hexdigest()[:24]
            if decode_key not in self._decode_keys:
                decode.update(key=decode_key, received_at_utc=now.isoformat())
                self._decodes.appendleft(decode)
                self._decode_keys.add(decode_key)
                while len(self._decodes) > self._max_decodes:
                    self._decode_keys.discard(self._decodes.pop()["key"])
        self._publish_locked()
