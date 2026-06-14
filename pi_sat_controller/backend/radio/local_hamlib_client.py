from __future__ import annotations

import logging
import socket
import subprocess
from threading import RLock, Thread
from time import monotonic, sleep

from pi_sat_controller.backend.radio.rigctld_client import PersistentRigctldClient

LOGGER = logging.getLogger(__name__)


class LocalHamlibClient:
    """Local Hamlib CAT client backed by one managed rigctld instance."""

    def __init__(
        self,
        model_id: int,
        serial_port: str,
        baud: int,
        timeout_s: float = 2.0,
        target_vfo: str | None = None,
        debug_logging: bool = False,
        role_label: str = "rx",
    ) -> None:
        self.model_id = model_id
        self.serial_port = serial_port
        self.baud = baud
        self.timeout_s = timeout_s
        self.target_vfo = target_vfo
        self.debug_logging = debug_logging
        self.role_label = role_label
        self._lock = RLock()
        self._daemon: subprocess.Popen[str] | None = None
        self._daemon_port: int | None = None
        self._client: PersistentRigctldClient | None = None
        self._log_thread: Thread | None = None

    def get_frequency(self) -> int:
        with self._lock:
            client = self._ensure_client()
            return client.get_frequency()

    def get_frequency_on_vfo(self, vfo: str | None) -> int:
        with self._lock:
            client = self._ensure_client()
            if vfo:
                client.select_vfo(vfo)
            return client.get_frequency()

    def set_frequency(self, frequency_hz: int) -> None:
        with self._lock:
            client = self._ensure_client()
            try:
                client.set_frequency(frequency_hz)
                return
            except Exception as exc:
                LOGGER.warning(
                    "local_hamlib role=%s frequency ack failed model_id=%s serial_port=%s target_hz=%s error=%s",
                    self.role_label,
                    self.model_id,
                    self.serial_port,
                    frequency_hz,
                    exc,
                )
                current_frequency = client.get_frequency()
                if current_frequency == frequency_hz:
                    LOGGER.info(
                        "local_hamlib role=%s frequency verified_by_readback target_hz=%s",
                        self.role_label,
                        frequency_hz,
                    )
                    return
                raise

    def set_frequency_on_vfo(self, vfo: str | None, frequency_hz: int) -> None:
        with self._lock:
            client = self._ensure_client()
            if vfo:
                client.select_vfo(vfo)
            try:
                client.set_frequency(frequency_hz)
                return
            except Exception as exc:
                LOGGER.warning(
                    "local_hamlib role=%s frequency ack failed model_id=%s serial_port=%s vfo=%s target_hz=%s error=%s",
                    self.role_label,
                    self.model_id,
                    self.serial_port,
                    vfo,
                    frequency_hz,
                    exc,
                )
                current_frequency = client.get_frequency()
                if current_frequency == frequency_hz:
                    LOGGER.info(
                        "local_hamlib role=%s frequency verified_by_readback vfo=%s target_hz=%s",
                        self.role_label,
                        vfo,
                        frequency_hz,
                    )
                    return
                raise

    def set_frequency_on_vfo_and_restore(
        self,
        vfo: str | None,
        frequency_hz: int,
        restore_vfo: str | None,
    ) -> None:
        with self._lock:
            client = self._ensure_client()
            if vfo:
                client.select_vfo(vfo)
            try:
                try:
                    client.set_frequency(frequency_hz)
                except Exception as exc:
                    LOGGER.warning(
                        "local_hamlib role=%s frequency ack failed model_id=%s serial_port=%s vfo=%s target_hz=%s error=%s",
                        self.role_label,
                        self.model_id,
                        self.serial_port,
                        vfo,
                        frequency_hz,
                        exc,
                    )
                    current_frequency = client.get_frequency()
                    if current_frequency != frequency_hz:
                        raise
                    LOGGER.info(
                        "local_hamlib role=%s frequency verified_by_readback vfo=%s target_hz=%s",
                        self.role_label,
                        vfo,
                        frequency_hz,
                    )
            finally:
                if restore_vfo:
                    client.select_vfo(restore_vfo)

    def set_mode(self, mode: str, passband_hz: int = 0) -> None:
        with self._lock:
            client = self._ensure_client()
            client.set_mode(mode, passband_hz)

    def set_mode_on_vfo(self, vfo: str | None, mode: str, passband_hz: int = 0) -> None:
        with self._lock:
            client = self._ensure_client()
            if vfo:
                client.select_vfo(vfo)
            client.set_mode(mode, passband_hz)

    def set_mode_on_vfo_and_restore(
        self,
        vfo: str | None,
        mode: str,
        passband_hz: int = 0,
        restore_vfo: str | None = None,
    ) -> None:
        with self._lock:
            client = self._ensure_client()
            if vfo:
                client.select_vfo(vfo)
            try:
                client.set_mode(mode, passband_hz)
            finally:
                if restore_vfo:
                    client.select_vfo(restore_vfo)

    def select_vfo(self, vfo: str) -> None:
        with self._lock:
            client = self._ensure_client()
            client.select_vfo(vfo)

    def close(self) -> None:
        with self._lock:
            if self._client is not None:
                self._client.close()
                self._client = None
            if self._daemon is not None:
                self._daemon.terminate()
                try:
                    self._daemon.wait(timeout=2.0)
                except subprocess.TimeoutExpired:
                    self._daemon.kill()
                    self._daemon.wait(timeout=2.0)
                self._daemon = None
            self._daemon_port = None

    def _ensure_client(self) -> PersistentRigctldClient:
        if self._client is not None and self._daemon is not None and self._daemon.poll() is None:
            return self._client
        self.close()
        port = _find_free_port()
        command = [
            "rigctld",
            "-m",
            str(self.model_id),
            "-r",
            self.serial_port,
            "-s",
            str(self.baud),
            "-t",
            str(port),
        ]
        if self.debug_logging:
            command.insert(1, "-vvvvv")
        LOGGER.info(
            "local_hamlib role=%s starting_rigctld model_id=%s serial_port=%s baud=%s port=%s debug=%s command=%s",
            self.role_label,
            self.model_id,
            self.serial_port,
            self.baud,
            port,
            self.debug_logging,
            command,
        )
        self._daemon = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE if self.debug_logging else subprocess.DEVNULL,
            stderr=subprocess.STDOUT if self.debug_logging else subprocess.DEVNULL,
            text=True,
            bufsize=1,
        )
        if self.debug_logging and self._daemon.stdout is not None:
            self._log_thread = Thread(
                target=_drain_rigctld_logs,
                args=(self._daemon.stdout, self.role_label),
                name="rigctld-log-drain",
                daemon=True,
            )
            self._log_thread.start()

        deadline = monotonic() + max(self.timeout_s, 5.0)
        last_error: Exception | None = None
        while monotonic() < deadline:
            if self._daemon.poll() is not None:
                raise RuntimeError(f"rigctld exited early with code {self._daemon.returncode}")
            client = PersistentRigctldClient(
                "127.0.0.1",
                port,
                self.timeout_s,
                self.debug_logging,
                role_label=self.role_label,
            )
            try:
                client.connect()
            except Exception as exc:
                last_error = exc
                sleep(0.1)
                continue
            self._client = client
            self._daemon_port = port
            LOGGER.info("local_hamlib role=%s rigctld_ready port=%s", self.role_label, port)
            return client
        raise RuntimeError(f"rigctld startup timed out: {last_error}")


def _find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _drain_rigctld_logs(stream, role_label: str) -> None:
    for line in stream:
        LOGGER.info("rigctld_raw role=%s %s", role_label, line.rstrip())
