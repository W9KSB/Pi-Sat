from __future__ import annotations

import logging
import socket
from types import TracebackType

LOGGER = logging.getLogger(__name__)


class RigctldClient:
    def __init__(
        self,
        host: str,
        port: int,
        timeout_s: float = 2.0,
        target_vfo: str | None = None,
        debug_logging: bool = False,
    ) -> None:
        self.host = host
        self.port = port
        self.timeout_s = timeout_s
        self.target_vfo = target_vfo
        self.debug_logging = debug_logging

    def _request(self, command: str) -> str:
        with socket.create_connection((self.host, self.port), self.timeout_s) as sock:
            if self.debug_logging:
                LOGGER.info("hamlib_socket_request host=%s port=%s command=%s", self.host, self.port, command)
            sock.sendall(command.encode("ascii") + b"\n")
            response = sock.recv(4096).decode("ascii").strip()
            if self.debug_logging:
                LOGGER.info("hamlib_socket_response host=%s port=%s command=%s response=%s", self.host, self.port, command, response)
            return response

    def get_frequency(self) -> int:
        return int(self._request("f"))

    def set_frequency(self, frequency_hz: int) -> None:
        response = self._request(f"F {frequency_hz}")
        if response and response != "RPRT 0":
            raise RuntimeError(f"rigctld rejected frequency set: {response}")

    def set_mode(self, mode: str, passband_hz: int = 0) -> None:
        response = self._request(f"M {mode} {passband_hz}")
        if response and response != "RPRT 0":
            raise RuntimeError(f"rigctld rejected mode set: {response}")

    def select_vfo(self, vfo: str) -> None:
        response = self._request(f"V {vfo}")
        if response and response != "RPRT 0":
            raise RuntimeError(f"rigctld rejected VFO select: {response}")


class PersistentRigctldClient:
    def __init__(self, host: str, port: int, timeout_s: float = 2.0, debug_logging: bool = False) -> None:
        self.host = host
        self.port = port
        self.timeout_s = timeout_s
        self.debug_logging = debug_logging
        self._socket: socket.socket | None = None
        self._reader = None

    def connect(self) -> None:
        if self._socket is not None:
            return

        self._socket = socket.create_connection((self.host, self.port), self.timeout_s)
        self._socket.settimeout(self.timeout_s)
        self._reader = self._socket.makefile("r", encoding="ascii", newline="\n")

    def close(self) -> None:
        if self._reader is not None:
            self._reader.close()
            self._reader = None
        if self._socket is not None:
            self._socket.close()
            self._socket = None

    def _request(self, command: str) -> str:
        self.connect()
        if self._socket is None or self._reader is None:
            raise RuntimeError("rigctld socket is not connected")

        try:
            if self.debug_logging:
                LOGGER.info("hamlib_socket_request host=%s port=%s command=%s", self.host, self.port, command)
            self._socket.sendall(command.encode("ascii") + b"\n")
            response = self._reader.readline()
        except Exception:
            self.close()
            raise

        if response == "":
            self.close()
            raise ConnectionError("rigctld closed the connection")
        normalized = response.strip()
        if self.debug_logging:
            LOGGER.info("hamlib_socket_response host=%s port=%s command=%s response=%s", self.host, self.port, command, normalized)
        return normalized

    def get_frequency(self) -> int:
        return int(self._request("f"))

    def set_frequency(self, frequency_hz: int) -> None:
        response = self._request(f"F {frequency_hz}")
        if response and response != "RPRT 0":
            raise RuntimeError(f"rigctld rejected frequency set: {response}")

    def set_mode(self, mode: str, passband_hz: int = 0) -> None:
        response = self._request(f"M {mode} {passband_hz}")
        if response and response != "RPRT 0":
            raise RuntimeError(f"rigctld rejected mode set: {response}")

    def select_vfo(self, vfo: str) -> None:
        response = self._request(f"V {vfo}")
        if response and response != "RPRT 0":
            raise RuntimeError(f"rigctld rejected VFO select: {response}")

    def __enter__(self) -> "PersistentRigctldClient":
        self.connect()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()
