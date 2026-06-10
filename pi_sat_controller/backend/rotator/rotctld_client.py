from __future__ import annotations

import socket
from dataclasses import dataclass


@dataclass(frozen=True)
class RotatorPosition:
    azimuth_deg: float
    elevation_deg: float


class RotctldClient:
    def __init__(self, host: str, port: int, timeout_s: float = 2.0) -> None:
        self.host = host
        self.port = port
        self.timeout_s = timeout_s

    def _request(self, command: str) -> str:
        with socket.create_connection((self.host, self.port), self.timeout_s) as sock:
            sock.sendall(command.encode("ascii") + b"\n")
            return sock.recv(4096).decode("ascii").strip()

    def get_position(self) -> RotatorPosition:
        response = self._request("p")
        parts = response.replace("\n", " ").split()
        if len(parts) < 2:
            raise RuntimeError(f"rotctld returned invalid position: {response}")
        return RotatorPosition(
            azimuth_deg=float(parts[0]),
            elevation_deg=float(parts[1]),
        )

    def set_position(self, azimuth_deg: float, elevation_deg: float) -> None:
        response = self._request(f"P {azimuth_deg:.2f} {elevation_deg:.2f}")
        if response and response != "RPRT 0":
            raise RuntimeError(f"rotctld rejected position set: {response}")

    def stop(self) -> None:
        response = self._request("S")
        if response and response != "RPRT 0":
            raise RuntimeError(f"rotctld rejected stop: {response}")
