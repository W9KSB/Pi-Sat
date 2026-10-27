"""Confirmed UDP and native accessibility control of the managed stock engine."""

from __future__ import annotations

import ipaddress
import json
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from datetime import datetime
from typing import Any

from .protocol import build_free_text, build_halt_tx, build_replay
from .accessibility import available as accessibility_available

_MANAGED_ID = "WSJT-X - PiSat-RX"


class WsjtxControlCancelled(RuntimeError):
    """A superseding operator action invalidated an in-flight control request."""


class WsjtxControl:
    def __init__(self, *, get_manager: Callable[[], Any], get_display: Callable[[], str | None],
                 get_pid: Callable[[], int | None], get_started_at: Callable[[], float | None],
                 get_environment: Callable[[], dict[str, str]] | None = None) -> None:
        self._get_manager = get_manager
        self._get_display = get_display
        self._get_pid = get_pid
        self._get_started_at = get_started_at
        self._get_environment = get_environment
        self._state_lock = threading.RLock()
        self._operation_lock = threading.Lock()
        self._generation: tuple[int | None, float | None] = (None, None)
        self._client_key: str | None = None
        self._prepared_message = ""
        self._tx_even: bool | None = None
        self._auto_sequence = False

    def _client(self) -> dict[str, Any] | None:
        generation = (self._get_pid(), self._get_started_at())
        with self._state_lock:
            if generation != self._generation:
                self._generation, self._client_key = generation, None
                self._prepared_message, self._tx_even = "", None
                self._auto_sequence = False
            client_key = self._client_key
        pid, started_at = generation
        if not pid or not started_at or not self._get_display():
            return None
        manager = self._get_manager()
        if manager is None:
            return None
        if client_key:
            client = manager.client_snapshot(client_key)
            return client if client and client.get("connected") else None
        candidates = []
        for client in manager.client_candidates():
            try:
                first_seen = datetime.fromisoformat(client.get("first_seen_utc", "")).timestamp()
                local = ipaddress.ip_address(client.get("host", "")).is_loopback
            except (TypeError, ValueError):
                continue
            if (client.get("id") == _MANAGED_ID and client.get("connected") and local
                    and first_seen >= started_at and client.get("status")):
                candidates.append(client)
        if len(candidates) != 1:
            return None
        with self._state_lock:
            if generation == self._generation:
                self._client_key = candidates[0]["key"]
        return candidates[0]

    def available(self) -> bool:
        return bool(accessibility_available() and self._client())

    def status(self) -> dict[str, Any]:
        client = self._client()
        status = dict(client.get("status") or {}) if client else {}
        with self._state_lock:
            prepared_message = self._prepared_message
            tx_even = self._tx_even
        return {"connected": client is not None, "client_key": client.get("key") if client else None,
                "status_sequence": client.get("status_sequence", 0) if client else 0,
                "tx_enabled": bool(status.get("tx_enabled")),
                "transmitting": bool(status.get("transmitting")),
                "tx_message": status.get("tx_message", ""), "mode": status.get("mode", ""),
                "prepared_message": prepared_message, "tx_even": tx_even,
                "tx_df_hz": status.get("tx_df_hz"), "rx_df_hz": status.get("rx_df_hz")}

    def _require_client(self) -> dict[str, Any]:
        client = self._client()
        if client is None:
            raise RuntimeError("The managed WSJT-X engine has not reported a fresh local status")
        return client

    def request_status(self) -> bool:
        """Request current UDP readback without waiting or taking the command lock."""
        client = self._client()
        if client is None:
            return False
        datagram = build_replay(client["id"], schema=client.get("schema", 3))
        return bool(self._get_manager().send_command(client["key"], datagram))

    def _send(self, builder: Callable[..., bytes], *args: Any) -> None:
        client = self._require_client()
        datagram = builder(client["id"], *args, schema=client.get("schema", 3))
        if not self._get_manager().send_command(client["key"], datagram):
            raise RuntimeError("The managed WSJT-X engine stopped accepting local commands")

    def _wait(self, predicate: Callable[[dict[str, Any]], bool], *, timeout: float = 2.0) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            status = self.status()
            if not status["connected"]:
                raise RuntimeError("The managed WSJT-X engine disconnected during control")
            if predicate(status):
                return status
            time.sleep(0.02)
        raise RuntimeError("WSJT-X did not confirm the requested control state")

    def _native_control(self, operation: str, value: bool | int | None = None) -> dict[str, Any]:
        if operation not in ("inspect", "options", "offset", "enabled"):
            raise ValueError("Invalid managed native control")
        if operation != "inspect":
            self._require_client()
        pid = self._get_pid()
        if type(pid) is not int or pid <= 0 or self._get_environment is None:
            raise RuntimeError("The managed private control session is unavailable")
        environment = {**self._get_environment(), "DISPLAY": self._get_display()}
        if not environment.get("DBUS_SESSION_BUS_ADDRESS", "").startswith(("unix:path=", "unix:abstract=")):
            raise RuntimeError("The managed private session bus is unavailable")
        # Native failures remain isolated from the long-running radio controller.
        command = [sys.executable, "-m", "pi_sat_controller.backend.wsjtx.accessibility",
                   str(pid), operation]
        if operation != "inspect":
            command.append(str(int(value)))
        result = subprocess.run(command, capture_output=True, text=True, timeout=8,
                                check=False, env=environment)
        if result.returncode:
            raise RuntimeError("Managed WSJT-X native control failed: " + result.stderr.strip()[:500])
        state = json.loads(result.stdout)
        if not isinstance(state, dict):
            raise RuntimeError("Managed WSJT-X native controls returned invalid state")
        return state

    def preflight(self) -> dict[str, Any]:
        """Verify the owned stock controls before TX can become available."""
        return self._native_control("inspect")

    @staticmethod
    def _check_cancelled(cancelled: Callable[[], bool] | None) -> None:
        if cancelled is not None and cancelled():
            raise WsjtxControlCancelled("Transmit control request was superseded")

    def _set_options(self, tx_even: bool) -> None:
        with self._state_lock:
            if self._auto_sequence is False and self._tx_even is tx_even:
                return
        options = self._native_control("options", tx_even)
        if options.get("auto_sequence") is not False or options.get("tx_even") is not tx_even:
            raise RuntimeError("Stock sequencing and transmit slot were not confirmed")
        with self._state_lock:
            self._auto_sequence, self._tx_even = False, tx_even

    def halt(self, *, force: bool = False) -> bool:
        status = self.status()
        if (not status["connected"]
                or (not force and not status["tx_enabled"] and not status["transmitting"])):
            return False
        self._send(build_halt_tx)
        self._wait(lambda value: not value["tx_enabled"] and not value["transmitting"])
        return True

    def set_enabled(
        self, enabled: bool, *, cancelled: Callable[[], bool] | None = None,
    ) -> dict[str, Any]:
        if type(enabled) is not bool:
            raise ValueError("TX enabled must be a boolean")
        if not enabled:
            self.halt()
            return self.status()
        with self._operation_lock:
            self._check_cancelled(cancelled)
            self._require_client()
            status = self.status()
            if status["tx_enabled"]:
                return status
            try:
                confirmed = self._native_control("enabled", True)
                if confirmed.get("tx_enabled") is not True or confirmed.get("auto_sequence") is not False:
                    raise RuntimeError("Stock transmit enable state was not confirmed")
                if cancelled is not None and cancelled():
                    # The helper may complete after an operator Halt. The RF
                    # bridge is already disabled, so repeat Halt at stock level.
                    self.halt(force=True)
                    raise WsjtxControlCancelled("Transmit enable request was superseded")
                return self._wait(lambda value: value["tx_enabled"])
            except WsjtxControlCancelled:
                raise
            except Exception:
                self._send(build_halt_tx)
                raise

    def configure(
        self, message: str, offset_hz: int, tx_even: bool, *,
        cancelled: Callable[[], bool] | None = None,
    ) -> dict[str, Any]:
        if type(offset_hz) is not int or not 200 <= offset_hz <= 5000 or type(tx_even) is not bool:
            raise ValueError("Invalid transmit audio offset or slot parity")
        build_free_text(_MANAGED_ID, message)
        with self._operation_lock:
            self._check_cancelled(cancelled)
            self._require_client()
            before = self.status()
            if before["transmitting"]:
                raise RuntimeError("Wait for the current WSJT-X transmission to finish before changing it")
            with self._state_lock:
                same_configuration = (
                    self._prepared_message == message
                    and self._tx_even is tx_even
                    and self._auto_sequence is False
                    and before["tx_df_hz"] == offset_hz
                )
            if same_configuration:
                return before
            try:
                current = self.status()
                with self._state_lock:
                    same_configuration = (
                        self._prepared_message == message
                        and self._tx_even is tx_even
                        and self._auto_sequence is False
                        and current["tx_df_hz"] == offset_hz
                    )
                if same_configuration:
                    return current
                if current["tx_enabled"]:
                    self.halt()
                self._check_cancelled(cancelled)
                # Re-read after Halt so parity and offset changes cannot race
                # an enabled stock sequence.
                current = self.status()
                old_rx, current_tx = current["rx_df_hz"], current["tx_df_hz"]
                if (type(old_rx) is not int or not 0 <= old_rx <= 5000
                        or type(current_tx) is not int or not 200 <= current_tx <= 5000):
                    raise RuntimeError("WSJT-X has not reported valid receive and transmit audio offsets")
                if current_tx != offset_hz:
                    self._check_cancelled(cancelled)
                    confirmed = self._native_control("offset", offset_hz)
                    if confirmed.get("tx_df_hz") != offset_hz or confirmed.get("rx_df_hz") != old_rx:
                        raise RuntimeError("Stock audio offsets were not confirmed")
                    self._check_cancelled(cancelled)
                    self._send(build_replay)
                self._check_cancelled(cancelled)
                self._set_options(tx_even)
                self._check_cancelled(cancelled)
                self._send(build_free_text, message)
                # UDP reports the encoded message only when stock modulation
                # starts. The radio bridge checks that actual message before PTT.
                with self._state_lock:
                    self._prepared_message, self._tx_even = message, tx_even
                self._check_cancelled(cancelled)
                return self.status()
            except WsjtxControlCancelled:
                raise
            except Exception:
                self.halt()
                self._prepared_message = ""
                raise
