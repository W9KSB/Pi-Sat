from __future__ import annotations

import logging
import re
import socket
from dataclasses import dataclass

LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class RotatorPosition:
    azimuth_deg: float
    elevation_deg: float


class RotctldClient:
    def __init__(self, host: str, port: int, timeout_s: float = 2.0, debug_logging: bool = False) -> None:
        self.host = host
        self.port = port
        self.timeout_s = timeout_s
        self.debug_logging = debug_logging

    def _request(self, command: str) -> str:
        with socket.create_connection((self.host, self.port), self.timeout_s) as sock:
            if self.debug_logging:
                LOGGER.info("rotctld_socket_request host=%s port=%s command=%s", self.host, self.port, command)
            sock.sendall(command.encode("ascii") + b"\n")
            response = sock.recv(4096).decode("ascii").strip()
            if self.debug_logging:
                LOGGER.info("rotctld_socket_response host=%s port=%s command=%s response=%s", self.host, self.port, command, response)
            return response

    def get_position(self) -> RotatorPosition:
        response = self._request("p")
        return _parse_rotator_position(response)

    def set_position(self, azimuth_deg: float, elevation_deg: float) -> None:
        response = self._request(f"P {azimuth_deg:.2f} {elevation_deg:.2f}")
        if response and response != "RPRT 0":
            raise RuntimeError(f"rotctld rejected position set: {response}")

    def stop(self) -> None:
        response = self._request("S")
        if response and response != "RPRT 0":
            raise RuntimeError(f"rotctld rejected stop: {response}")


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
