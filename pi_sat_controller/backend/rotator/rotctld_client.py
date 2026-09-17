from __future__ import annotations

import logging
import re
import socket
from dataclasses import dataclass
from threading import Lock
from time import monotonic

LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class RotatorPosition:
    azimuth_deg: float
    elevation_deg: float


class RotctldClient:
    """Hamlib rotctld client keeping one connection for the whole session.

    rotctld serves a command loop per connection and never writes unless a
    command was sent, so the socket is reused instead of reconnecting for every
    command. Calls are serialized because the tracker thread and the request
    threadpool both drive the same rotator.
    """

    def __init__(
        self,
        host: str,
        port: int,
        timeout_s: float = 2.0,
        debug_logging: bool = False,
        role_label: str = "rotator",
    ) -> None:
        self.host = host
        self.port = port
        self.timeout_s = timeout_s
        self.debug_logging = debug_logging
        self.role_label = role_label
        self._lock = Lock()
        self._socket: socket.socket | None = None

    def get_position(self) -> RotatorPosition:
        return _parse_rotator_position(self._request("p", expected_lines=2))

    def set_position(self, azimuth_deg: float, elevation_deg: float) -> None:
        response = self._request(f"P {azimuth_deg:.2f} {elevation_deg:.2f}")
        if response and response != "RPRT 0":
            raise RuntimeError(f"rotctld rejected position set: {response}")

    def stop(self) -> None:
        response = self._request("S")
        if response and response != "RPRT 0":
            raise RuntimeError(f"rotctld rejected stop: {response}")

    def close(self) -> None:
        """Ask rotctld to end this session, then drop the connection."""

        with self._lock:
            if self._socket is None:
                return
            try:
                self._exchange_locked("q")
            except (OSError, socket.timeout):
                # Some rotctld builds close immediately after q without a payload.
                pass
            finally:
                self._disconnect_locked()

    def _request(self, command: str, expected_lines: int = 1) -> str:
        with self._lock:
            reused = self._socket is not None
            try:
                return self._exchange_locked(command, expected_lines)
            except (OSError, socket.timeout):
                self._disconnect_locked()
                if not reused:
                    # Failing while connecting is an outage rather than a stale
                    # session, so the timeout budget is not spent twice.
                    raise
                return self._exchange_locked(command, expected_lines)

    def _exchange_locked(self, command: str, expected_lines: int = 1) -> str:
        sock = self._connect_locked()
        if self.debug_logging:
            LOGGER.info(
                "rotctld_socket_request role=%s host=%s port=%s command=%s",
                self.role_label,
                self.host,
                self.port,
                command,
            )
        try:
            sock.sendall(command.encode("ascii") + b"\n")
            response = self._read_locked(sock, expected_lines)
        except (OSError, socket.timeout):
            self._disconnect_locked()
            raise
        if self.debug_logging:
            LOGGER.info(
                "rotctld_socket_response role=%s host=%s port=%s command=%s response=%s",
                self.role_label,
                self.host,
                self.port,
                command,
                response,
            )
        return response

    def _connect_locked(self) -> socket.socket:
        if self._socket is None:
            self._socket = socket.create_connection(
                (self.host, self.port), self.timeout_s
            )
        return self._socket

    def _read_locked(self, sock: socket.socket, expected_lines: int) -> str:
        """Reads one reply, which may arrive across several segments."""

        buffer = b""
        deadline = monotonic() + self.timeout_s
        while True:
            if buffer.count(b"\n") >= expected_lines:
                break
            # A rejected command answers with a single status line whatever the
            # command normally returns, so it must not wait for the rest.
            if b"\n" in buffer and buffer.lstrip()[:4].upper() == b"RPRT":
                break
            remaining = deadline - monotonic()
            if remaining <= 0:
                break
            sock.settimeout(remaining)
            chunk = sock.recv(4096)
            if not chunk:
                raise ConnectionResetError(
                    f"rotctld closed the connection on {self.role_label}"
                )
            buffer += chunk
        return buffer.decode("ascii", "replace").strip()

    def _disconnect_locked(self) -> None:
        sock, self._socket = self._socket, None
        if sock is None:
            return
        try:
            sock.close()
        except OSError:
            pass


def _parse_rotator_position(response: str) -> RotatorPosition:
    text = response.strip()
    if not text:
        raise RuntimeError("rotctld returned empty position response")
    if text.upper().startswith("RPRT"):
        raise RuntimeError(f"rotctld returned error: {text}")

    az_match = re.search(r"AZ\s*=\s*([-+]?\d+(?:\.\d+)?)", text, re.IGNORECASE)
    el_match = re.search(r"EL\s*=\s*([-+]?\d+(?:\.\d+)?)", text, re.IGNORECASE)
    if az_match and el_match:
        return RotatorPosition(
            azimuth_deg=float(az_match.group(1)),
            elevation_deg=float(el_match.group(1)),
        )

    numeric_tokens: list[float] = []
    for token in re.split(r"[\s,]+", text.replace("\n", " ").strip()):
        cleaned = token.strip()
        if not cleaned or cleaned.upper() == "RPRT":
            continue
        if "=" in cleaned:
            _, _, cleaned = cleaned.partition("=")
            cleaned = cleaned.strip()
        try:
            numeric_tokens.append(float(cleaned))
        except ValueError:
            continue
        if len(numeric_tokens) >= 2:
            break

    if len(numeric_tokens) < 2:
        raise RuntimeError(f"rotctld returned invalid position: {response}")

    return RotatorPosition(
        azimuth_deg=numeric_tokens[0],
        elevation_deg=numeric_tokens[1],
    )
