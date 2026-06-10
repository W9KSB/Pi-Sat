from __future__ import annotations

import logging
import socket
import subprocess
from threading import RLock
from time import monotonic, sleep

from pi_sat_controller.backend.rotator.rotctld_client import RotatorPosition, RotctldClient

LOGGER = logging.getLogger(__name__)


class LocalRotctldClient:
    """Local Hamlib rotator client backed by one managed rotctld instance."""

    def __init__(
        self,
        model_id: int,
        serial_port: str,
        baud: int,
        timeout_s: float = 2.0,
    ) -> None:
        self.model_id = model_id
        self.serial_port = serial_port
        self.baud = baud
        self.timeout_s = timeout_s
        self._lock = RLock()
        self._daemon: subprocess.Popen[str] | None = None
        self._client: RotctldClient | None = None

    def get_position(self) -> RotatorPosition:
        with self._lock:
            return self._ensure_client().get_position()

    def set_position(self, azimuth_deg: float, elevation_deg: float) -> None:
        with self._lock:
            self._ensure_client().set_position(azimuth_deg, elevation_deg)

    def stop(self) -> None:
        with self._lock:
            self._ensure_client().stop()

    def close(self) -> None:
        with self._lock:
            self._client = None
            if self._daemon is not None:
                self._daemon.terminate()
                try:
                    self._daemon.wait(timeout=2.0)
                except subprocess.TimeoutExpired:
                    self._daemon.kill()
                    self._daemon.wait(timeout=2.0)
                self._daemon = None

    def _ensure_client(self) -> RotctldClient:
        if self._client is not None and self._daemon is not None and self._daemon.poll() is None:
            return self._client
        self.close()
        port = _find_free_port()
        command = [
            "rotctld",
            "-m",
            str(self.model_id),
            "-r",
            self.serial_port,
            "-s",
            str(self.baud),
            "-t",
            str(port),
        ]
        LOGGER.info(
            "local_rotctld starting model_id=%s serial_port=%s baud=%s port=%s command=%s",
            self.model_id,
            self.serial_port,
            self.baud,
            port,
            command,
        )
        self._daemon = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            text=True,
        )

        deadline = monotonic() + max(self.timeout_s, 5.0)
        last_error: Exception | None = None
        while monotonic() < deadline:
            if self._daemon.poll() is not None:
                raise RuntimeError(f"rotctld exited early with code {self._daemon.returncode}")
            client = RotctldClient("127.0.0.1", port, self.timeout_s)
            try:
                client.get_position()
            except Exception as exc:
                last_error = exc
                sleep(0.1)
                continue
            self._client = client
            LOGGER.info("local_rotctld ready port=%s", port)
            return client
        raise RuntimeError(f"rotctld startup timed out: {last_error}")


def _find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])
