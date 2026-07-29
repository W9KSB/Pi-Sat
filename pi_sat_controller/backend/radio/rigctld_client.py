from __future__ import annotations

import logging
import socket
from types import TracebackType

LOGGER = logging.getLogger(__name__)


def _parse_boolean_response(response: str, command_name: str) -> bool:
    value = response.splitlines()[0].strip() if response else ""
    if value in {"0", "1", "2", "3"}:
        return value != "0"
    raise RuntimeError(f"rigctld rejected {command_name}: {response or 'empty response'}")


def _parse_vfo_response(response: str) -> str:
    value = response.splitlines()[0].strip().upper() if response else ""
    if value and not value.startswith("RPRT"):
        return value
    raise RuntimeError(f"rigctld rejected VFO read: {response or 'empty response'}")


def _parse_vfo_mode_response(response: str) -> bool:
    value = response.strip().upper()
    if value in {"1", "CHKVFO 1"}:
        return True
    if value in {"0", "CHKVFO 0"}:
        return False
    raise RuntimeError(
        f"rigctld returned an invalid VFO mode response: {response or 'empty response'}"
    )


class RigctldClient:
    def __init__(
        self,
        host: str,
        port: int,
        timeout_s: float = 2.0,
        target_vfo: str | None = None,
        debug_logging: bool = False,
        role_label: str = "rx",
    ) -> None:
        self.host = host
        self.port = port
        self.timeout_s = timeout_s
        self.target_vfo = target_vfo
        self.debug_logging = debug_logging
        self.role_label = role_label

    def _request(self, command: str) -> str:
        with socket.create_connection((self.host, self.port), self.timeout_s) as sock:
            if self.debug_logging:
                LOGGER.info(
                    "hamlib_socket_request role=%s host=%s port=%s command=%s",
                    self.role_label,
                    self.host,
                    self.port,
                    command,
                )
            sock.sendall(command.encode("ascii") + b"\n")
            response = sock.recv(4096).decode("ascii").strip()
            if self.debug_logging:
                LOGGER.info(
                    "hamlib_socket_response role=%s host=%s port=%s command=%s response=%s",
                    self.role_label,
                    self.host,
                    self.port,
                    command,
                    response,
                )
            return response

    def get_frequency(self) -> int:
        return int(self._request("f"))

    def get_ptt(self) -> bool:
        return _parse_boolean_response(self._request("t"), "PTT read")

    def get_vfo(self) -> str:
        return _parse_vfo_response(self._request("v"))

    def check_vfo_mode(self) -> bool:
        return _parse_vfo_mode_response(self._request(r"\chk_vfo"))

    def set_cache_timeout_ms(self, timeout_ms: int) -> None:
        response = self._request(fr"\set_cache {max(0, int(timeout_ms))}")
        if response and response != "RPRT 0":
            raise RuntimeError(f"rigctld rejected cache timeout set: {response}")

    def set_frequency(self, frequency_hz: int) -> None:
        response = self._request(f"F {frequency_hz}")
        if response and response != "RPRT 0":
            raise RuntimeError(f"rigctld rejected frequency set: {response}")

    def set_mode(self, mode: str, passband_hz: int = 0) -> None:
        response = self._request(f"M {mode} {passband_hz}")
        if response and response != "RPRT 0":
            raise RuntimeError(f"rigctld rejected mode set: {response}")

    def set_ctcss_tone(self, tone_tenths_hz: int) -> None:
        response = self._request(f"C {tone_tenths_hz}")
        if response and response != "RPRT 0":
            raise RuntimeError(f"rigctld rejected CTCSS tone set: {response}")

    def set_tone_enabled(self, enabled: bool) -> None:
        response = self._request(f"U TONE {1 if enabled else 0}")
        if response and response != "RPRT 0":
            raise RuntimeError(f"rigctld rejected CTCSS encoder state: {response}")

    def set_split(self, enabled: bool, tx_vfo: str | None = None) -> None:
        command = f"S {1 if enabled else 0}"
        if tx_vfo:
            command = f"{command} {tx_vfo}"
        response = self._request(command)
        if response and response != "RPRT 0":
            raise RuntimeError(f"rigctld rejected split set: {response}")

    def set_split_frequency(self, frequency_hz: int) -> None:
        response = self._request(f"I {frequency_hz}")
        if response and response != "RPRT 0":
            raise RuntimeError(f"rigctld rejected split frequency set: {response}")

    def set_split_mode(self, mode: str, passband_hz: int = 0) -> None:
        response = self._request(f"X {mode} {passband_hz}")
        if response and response != "RPRT 0":
            raise RuntimeError(f"rigctld rejected split mode set: {response}")

    def select_vfo(self, vfo: str) -> None:
        response = self._request(f"V {vfo}")
        if response and response != "RPRT 0":
            raise RuntimeError(f"rigctld rejected VFO select: {response}")

class PersistentRigctldClient:
    def __init__(
        self,
        host: str,
        port: int,
        timeout_s: float = 2.0,
        debug_logging: bool = False,
        role_label: str = "rx",
        target_vfo: str | None = None,
        vfo_mode: bool = False,
    ) -> None:
        self.host = host
        self.port = port
        self.timeout_s = timeout_s
        self.debug_logging = debug_logging
        self.role_label = role_label
        self.target_vfo = target_vfo
        self.vfo_mode = vfo_mode
        self._socket: socket.socket | None = None
        self._reader = None
        self._broken = False

    @property
    def is_broken(self) -> bool:
        return self._broken

    def connect(self) -> None:
        if self._socket is not None:
            return

        self._open_socket()

    def _open_socket(self) -> None:
        self._socket = socket.create_connection((self.host, self.port), self.timeout_s)
        self._socket.settimeout(self.timeout_s)
        self._reader = self._socket.makefile("r", encoding="ascii", newline="\n")
        self._broken = False

    def close(self) -> None:
        if self._reader is not None:
            self._reader.close()
            self._reader = None
        if self._socket is not None:
            self._socket.close()
            self._socket = None

    def _mark_broken(self) -> None:
        try:
            self.close()
        finally:
            self._broken = True

    def _request(self, command: str) -> str:
        self.connect()
        if self._socket is None or self._reader is None:
            raise RuntimeError("rigctld socket is not connected")

        try:
            if self.debug_logging:
                LOGGER.info(
                    "hamlib_socket_request role=%s host=%s port=%s command=%s",
                    self.role_label,
                    self.host,
                    self.port,
                    command,
                )
            self._socket.sendall(command.encode("ascii") + b"\n")
            response = self._reader.readline()
        except Exception:
            self._mark_broken()
            raise

        if response == "":
            self._mark_broken()
            raise ConnectionError("rigctld closed the connection")
        normalized = response.strip()
        if self.debug_logging:
            LOGGER.info(
                "hamlib_socket_response role=%s host=%s port=%s command=%s response=%s",
                self.role_label,
                self.host,
                self.port,
                command,
                normalized,
            )
        return normalized

    def get_frequency(self) -> int:
        return int(self._request("f"))

    def get_frequency_on_vfo(self, vfo: str) -> int:
        return int(self._request(f"f {vfo}"))

    def get_ptt(self) -> bool:
        return _parse_boolean_response(self._request("t"), "PTT read")

    def get_ptt_on_vfo(self, vfo: str) -> bool:
        return _parse_boolean_response(self._request(f"t {vfo}"), "PTT read")

    def get_vfo(self) -> str:
        return _parse_vfo_response(self._request("v"))

    def check_vfo_mode(self) -> bool:
        return _parse_vfo_mode_response(self._request(r"\chk_vfo"))

    def set_cache_timeout_ms(self, timeout_ms: int) -> None:
        response = self._request(fr"\set_cache {max(0, int(timeout_ms))}")
        if response and response != "RPRT 0":
            raise RuntimeError(f"rigctld rejected cache timeout set: {response}")

    def set_frequency(self, frequency_hz: int) -> None:
        response = self._request(f"F {frequency_hz}")
        if response and response != "RPRT 0":
            raise RuntimeError(f"rigctld rejected frequency set: {response}")

    def set_frequency_on_vfo(self, vfo: str, frequency_hz: int) -> None:
        response = self._request(f"F {vfo} {frequency_hz}")
        if response and response != "RPRT 0":
            raise RuntimeError(f"rigctld rejected VFO frequency set: {response}")

    def set_mode(self, mode: str, passband_hz: int = 0) -> None:
        response = self._request(f"M {mode} {passband_hz}")
        if response and response != "RPRT 0":
            raise RuntimeError(f"rigctld rejected mode set: {response}")

    def set_mode_on_vfo(self, vfo: str, mode: str, passband_hz: int = 0) -> None:
        response = self._request(f"M {vfo} {mode} {passband_hz}")
        if response and response != "RPRT 0":
            raise RuntimeError(f"rigctld rejected VFO mode set: {response}")

    def set_ctcss_tone(self, tone_tenths_hz: int) -> None:
        response = self._request(f"C {tone_tenths_hz}")
        if response and response != "RPRT 0":
            raise RuntimeError(f"rigctld rejected CTCSS tone set: {response}")

    def set_ctcss_tone_on_vfo(self, vfo: str, tone_tenths_hz: int) -> None:
        response = self._request(f"C {vfo} {tone_tenths_hz}")
        if response and response != "RPRT 0":
            raise RuntimeError(f"rigctld rejected VFO CTCSS tone set: {response}")

    def set_tone_enabled(self, enabled: bool) -> None:
        response = self._request(f"U TONE {1 if enabled else 0}")
        if response and response != "RPRT 0":
            raise RuntimeError(f"rigctld rejected CTCSS encoder state: {response}")

    def set_tone_enabled_on_vfo(self, vfo: str, enabled: bool) -> None:
        response = self._request(f"U {vfo} TONE {1 if enabled else 0}")
        if response and response != "RPRT 0":
            raise RuntimeError(f"rigctld rejected VFO CTCSS encoder state: {response}")

    def set_split(self, enabled: bool, tx_vfo: str | None = None) -> None:
        command = f"S {1 if enabled else 0}"
        if tx_vfo:
            command = f"{command} {tx_vfo}"
        response = self._request(command)
        if response and response != "RPRT 0":
            raise RuntimeError(f"rigctld rejected split set: {response}")

    def set_split_on_vfo(
        self,
        rx_vfo: str,
        enabled: bool,
        tx_vfo: str | None = None,
    ) -> None:
        command = f"S {rx_vfo} {1 if enabled else 0}"
        if tx_vfo:
            command = f"{command} {tx_vfo}"
        response = self._request(command)
        if response and response != "RPRT 0":
            raise RuntimeError(f"rigctld rejected VFO split set: {response}")

    def set_split_frequency(self, frequency_hz: int) -> None:
        response = self._request(f"I {frequency_hz}")
        if response and response != "RPRT 0":
            raise RuntimeError(f"rigctld rejected split frequency set: {response}")

    def set_split_mode(self, mode: str, passband_hz: int = 0) -> None:
        response = self._request(f"X {mode} {passband_hz}")
        if response and response != "RPRT 0":
            raise RuntimeError(f"rigctld rejected split mode set: {response}")

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
