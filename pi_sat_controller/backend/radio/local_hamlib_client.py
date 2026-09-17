from __future__ import annotations

from collections import defaultdict, deque
import logging
import socket
import subprocess
from threading import Lock, RLock, Thread
from time import monotonic, sleep

from pi_sat_controller.backend.radio.hamlib_async import (
    HamlibAsyncCapability,
    HamlibAsyncStateListener,
    probe_hamlib_async_capability,
)
from pi_sat_controller.backend.radio.radio_state import (
    ASYNC_ROLE_ROUTE_MAX_DISTANCE_HZ,
    ASYNC_ROLE_ROUTE_MIN_ADVANTAGE_HZ,
    AsyncCapabilityState,
    RecentCommandHistory,
    RadioStateClassification,
    RadioStateEvent,
    RadioStateProperty,
    normalize_event_vfo,
)
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
        vfo_mode: bool = False,
        state_updates: str = "automatic",
    ) -> None:
        self.model_id = model_id
        self.serial_port = serial_port
        self.baud = baud
        self.timeout_s = timeout_s
        self.target_vfo = target_vfo
        self.debug_logging = debug_logging
        self.role_label = role_label
        self.vfo_mode = vfo_mode
        self.state_updates = _normalize_state_updates(state_updates)
        self._lock = RLock()
        self._event_lock = Lock()
        self._daemon: subprocess.Popen[str] | None = None
        self._daemon_port: int | None = None
        self._client: PersistentRigctldClient | None = None
        self._log_thread: Thread | None = None
        self._connection_generation = 0
        self._async_listener: HamlibAsyncStateListener | None = None
        self._async_capability = HamlibAsyncCapability(
            state=AsyncCapabilityState.UNSUPPORTED,
            hamlib_version=None,
            backend_supported=False,
            reason=(
                "Polling Only is selected."
                if self.state_updates == "polling"
                else "Async capability has not been probed yet."
            ),
        )
        self._recent_commands = RecentCommandHistory()
        self._role_vfos: dict[str, str | None] = {}
        self._role_frequency_hints: dict[str, int] = {}
        self._event_queues: dict[str, deque[RadioStateEvent]] = defaultdict(
            lambda: deque(maxlen=64)
        )
        self._verified_properties: dict[str, set[RadioStateProperty]] = defaultdict(set)
        default_role = _normalize_role(role_label)
        if default_role is not None:
            self._role_vfos[default_role] = normalize_event_vfo(target_vfo)

    @property
    def connection_generation(self) -> int:
        return self._connection_generation

    def ensure_connected(self) -> int:
        with self._lock:
            self._ensure_client()
            return self._connection_generation

    def register_role_vfo(self, role: str, vfo: str | None) -> None:
        normalized_role = _normalize_role(role)
        if normalized_role is None:
            raise ValueError(f"Unsupported radio role: {role}")
        with self._event_lock:
            self._role_vfos[normalized_role] = normalize_event_vfo(vfo)

    def drain_radio_state_events(self, role: str | None = None) -> list[RadioStateEvent]:
        self._expire_recent_commands()
        normalized_role = _normalize_role(role) or _normalize_role(self.role_label)
        if normalized_role is None:
            return []
        with self._event_lock:
            queue = self._event_queues[normalized_role]
            events = list(queue)
            queue.clear()
            return events

    def is_async_property_verified(
        self,
        property: RadioStateProperty | str,
        role: str | None = None,
    ) -> bool:
        self._expire_recent_commands()
        normalized_role = _normalize_role(role) or _normalize_role(self.role_label)
        try:
            normalized_property = RadioStateProperty(property)
        except ValueError:
            return False
        listener = self._async_listener
        if normalized_role is None or listener is None or not listener.running:
            return False
        with self._event_lock:
            return normalized_property in self._verified_properties[normalized_role]

    def async_status(self, role: str | None = None) -> dict[str, object]:
        self._expire_recent_commands()
        normalized_role = _normalize_role(role) or _normalize_role(self.role_label)
        listener = self._async_listener
        with self._event_lock:
            verified = sorted(
                property.value
                for property in self._verified_properties.get(normalized_role or "", set())
            )
        available = bool(
            self._async_capability.backend_supported
            and listener is not None
            and listener.running
        )
        state = AsyncCapabilityState.UNSUPPORTED
        if available:
            state = AsyncCapabilityState.VERIFIED if verified else AsyncCapabilityState.AVAILABLE
        reason = self._async_capability.reason
        if (
            self._async_capability.backend_supported
            and listener is not None
            and not listener.running
        ):
            reason = (
                f"Async listener is not healthy: {listener.last_error}"
                if listener.last_error
                else "Async listener is not running."
            )
        status = {
            "preference": self.state_updates,
            "state": state.value,
            "available": available,
            "verified_properties": verified,
            "healthy": available,
            **self._async_capability.to_dict(),
        }
        status["state"] = state.value
        status["reason"] = reason
        if listener is not None:
            status.update(listener.status())
        else:
            status.update(
                {
                    "listener_running": False,
                    "last_async_event_monotonic": None,
                    "listener_error": None,
                }
            )
        return status

    def get_frequency(self) -> int:
        with self._lock:
            client = self._ensure_client()
            if self.vfo_mode and self.target_vfo:
                frequency_hz = client.get_frequency_on_vfo(self.target_vfo)
            else:
                frequency_hz = client.get_frequency()
            self._seed_async_observed(
                RadioStateProperty.FREQUENCY,
                frequency_hz,
                self.target_vfo,
            )
            return frequency_hz

    def get_ptt(self) -> bool:
        with self._lock:
            client = self._ensure_client()
            return client.get_ptt()

    def get_vfo(self) -> str:
        with self._lock:
            client = self._ensure_client()
            return client.get_vfo()

    def get_frequency_on_vfo(self, vfo: str | None) -> int:
        with self._lock:
            client = self._ensure_client()
            if vfo:
                if self.vfo_mode:
                    frequency_hz = client.get_frequency_on_vfo(vfo)
                    self._seed_async_observed(RadioStateProperty.FREQUENCY, frequency_hz, vfo)
                    return frequency_hz
                client.select_vfo(vfo)
            frequency_hz = client.get_frequency()
            self._seed_async_observed(RadioStateProperty.FREQUENCY, frequency_hz, vfo)
            return frequency_hz

    def get_ptt_on_vfo(self, vfo: str) -> bool:
        with self._lock:
            client = self._ensure_client()
            if self.vfo_mode:
                return client.get_ptt_on_vfo(vfo)
            return client.get_ptt()

    def set_frequency(self, frequency_hz: int) -> None:
        with self._lock:
            client = self._ensure_client()
            self._record_recent_command(
                RadioStateProperty.FREQUENCY,
                frequency_hz,
                self.target_vfo,
            )
            if self.vfo_mode and self.target_vfo:
                client.set_frequency_on_vfo(self.target_vfo, frequency_hz)
                return
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
            self._record_recent_command(RadioStateProperty.FREQUENCY, frequency_hz, vfo)
            if vfo and self.vfo_mode:
                client.set_frequency_on_vfo(vfo, frequency_hz)
                return
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
            self._record_recent_command(RadioStateProperty.FREQUENCY, frequency_hz, vfo)
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
            self._record_recent_command(RadioStateProperty.MODE, mode.upper(), self.target_vfo)
            client.set_mode(mode, passband_hz)

    def set_split(self, enabled: bool, tx_vfo: str | None = None) -> None:
        with self._lock:
            client = self._ensure_client()
            client.set_split(enabled, tx_vfo)

    def set_split_on_vfo(
        self,
        rx_vfo: str,
        enabled: bool,
        tx_vfo: str | None = None,
    ) -> None:
        with self._lock:
            client = self._ensure_client()
            if self.vfo_mode:
                client.set_split_on_vfo(rx_vfo, enabled, tx_vfo)
                return
            client.set_split(enabled, tx_vfo)

    def set_split_frequency(self, frequency_hz: int) -> None:
        with self._lock:
            client = self._ensure_client()
            self._recent_commands.record(
                RadioStateProperty.FREQUENCY,
                frequency_hz,
                role="tx",
                vfo=self.target_vfo,
                source="pi_sat_split",
            )
            client.set_split_frequency(frequency_hz)

    def set_split_mode(self, mode: str, passband_hz: int = 0) -> None:
        with self._lock:
            client = self._ensure_client()
            self._recent_commands.record(
                RadioStateProperty.MODE,
                mode.upper(),
                role="tx",
                vfo=self.target_vfo,
                source="pi_sat_split",
            )
            client.set_split_mode(mode, passband_hz)

    def set_mode_on_vfo(self, vfo: str | None, mode: str, passband_hz: int = 0) -> None:
        with self._lock:
            client = self._ensure_client()
            self._record_recent_command(RadioStateProperty.MODE, mode.upper(), vfo)
            if vfo:
                if self.vfo_mode:
                    client.set_mode_on_vfo(vfo, mode, passband_hz)
                    return
                client.select_vfo(vfo)
            client.set_mode(mode, passband_hz)

    def set_ctcss_tone_on_vfo(
        self,
        vfo: str | None,
        tone_tenths_hz: int,
    ) -> None:
        with self._lock:
            client = self._ensure_client()
            if vfo and self.vfo_mode:
                client.set_ctcss_tone_on_vfo(vfo, tone_tenths_hz)
                return
            if vfo:
                client.select_vfo(vfo)
            client.set_ctcss_tone(tone_tenths_hz)

    def set_tone_enabled_on_vfo(self, vfo: str | None, enabled: bool) -> None:
        with self._lock:
            client = self._ensure_client()
            if vfo and self.vfo_mode:
                client.set_tone_enabled_on_vfo(vfo, enabled)
                return
            if vfo:
                client.select_vfo(vfo)
            client.set_tone_enabled(enabled)

    def set_mode_on_vfo_and_restore(
        self,
        vfo: str | None,
        mode: str,
        passband_hz: int = 0,
        restore_vfo: str | None = None,
    ) -> None:
        with self._lock:
            client = self._ensure_client()
            self._record_recent_command(RadioStateProperty.MODE, mode.upper(), vfo)
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
            if self._async_listener is not None:
                self._async_listener.stop()
                self._async_listener = None
            self._recent_commands.clear()
            with self._event_lock:
                self._event_queues.clear()
                self._verified_properties.clear()
                self._role_frequency_hints.clear()

    def _ensure_client(self) -> PersistentRigctldClient:
        if (
            self._client is not None
            and not self._client.is_broken
            and self._daemon is not None
            and self._daemon.poll() is None
        ):
            return self._client
        if self._client is not None and self._client.is_broken:
            LOGGER.warning(
                "local_hamlib role=%s restarting_rigctld_after_transport_failure "
                "model_id=%s serial_port=%s",
                self.role_label,
                self.model_id,
                self.serial_port,
            )
        self.close()
        async_port = self._prepare_async_listener()
        try:
            return self._start_rigctld(async_port)
        except Exception as exc:
            if async_port is None:
                raise
            LOGGER.warning(
                "local_hamlib role=%s async_setup_failed error=%s falling_back_to_polling",
                self.role_label,
                exc,
            )
            self.close()
            self._async_capability = HamlibAsyncCapability(
                state=AsyncCapabilityState.UNSUPPORTED,
                hamlib_version=self._async_capability.hamlib_version,
                backend_supported=False,
                reason=f"Async rigctld setup failed: {exc}",
            )
            return self._start_rigctld(None)

    def _start_rigctld(self, async_port: int | None) -> PersistentRigctldClient:
        port = _find_free_port()
        command = _build_rigctld_command(
            model_id=self.model_id,
            serial_port=self.serial_port,
            baud=self.baud,
            tcp_port=port,
            async_port=async_port,
            vfo_mode=self.vfo_mode,
            debug_logging=self.debug_logging,
        )
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
            encoding="utf-8",
            errors="backslashreplace",
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

        # Hamlib waits two seconds before starting a supported async backend's
        # reader thread, and rigctld does not bind its TCP listener until after
        # rig_open() returns. Leave room for that fixed delay plus radio setup.
        startup_timeout_s = max(
            self.timeout_s,
            8.0 if async_port is not None else 5.0,
        )
        deadline = monotonic() + startup_timeout_s
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
                target_vfo=self.target_vfo,
                vfo_mode=self.vfo_mode,
            )
            try:
                client.connect()
                if self.vfo_mode and not client.check_vfo_mode():
                    client.close()
                    raise RuntimeError("rigctld did not enable VFO-addressed command mode")
                try:
                    client.set_cache_timeout_ms(0)
                except RuntimeError as exc:
                    LOGGER.warning(
                        "local_hamlib role=%s unable_to_disable_rigctld_cache "
                        "error=%s",
                        self.role_label,
                        exc,
                    )
            except Exception as exc:
                last_error = exc
                sleep(0.1)
                continue
            self._client = client
            self._daemon_port = port
            self._connection_generation += 1
            LOGGER.info("local_hamlib role=%s rigctld_ready port=%s", self.role_label, port)
            return client
        raise RuntimeError(f"rigctld startup timed out: {last_error}")

    def _prepare_async_listener(self) -> int | None:
        if self.state_updates == "polling":
            self._async_capability = HamlibAsyncCapability(
                state=AsyncCapabilityState.UNSUPPORTED,
                hamlib_version=None,
                backend_supported=False,
                reason="Polling Only is selected.",
            )
            LOGGER.info("local_hamlib role=%s async_disabled_by_setting", self.role_label)
            return None
        capability = probe_hamlib_async_capability(self.model_id, self.timeout_s)
        self._async_capability = capability
        LOGGER.info(
            "local_hamlib role=%s hamlib_version=%s async_capability=%s reason=%s",
            self.role_label,
            capability.hamlib_version,
            capability.state.value,
            capability.reason,
        )
        if not capability.backend_supported:
            return None
        listener = HamlibAsyncStateListener(self._handle_async_event)
        port = listener.start()
        self._async_listener = listener
        LOGGER.info(
            "local_hamlib role=%s async_listener_started host=127.0.0.1 port=%s",
            self.role_label,
            port,
        )
        return port

    def _seed_async_observed(
        self,
        property: RadioStateProperty,
        value: object,
        vfo: str | None,
    ) -> None:
        if property == RadioStateProperty.FREQUENCY:
            self._remember_role_frequency(self._role_for_vfo(vfo), value)
        listener = self._async_listener
        if listener is not None:
            listener.seed_observed(property, value, vfo)

    def _record_recent_command(
        self,
        property: RadioStateProperty,
        value: object,
        vfo: str | None,
    ) -> None:
        if self._async_listener is None:
            return
        role = self._role_for_vfo(vfo)
        if property == RadioStateProperty.FREQUENCY:
            self._remember_role_frequency(role, value)
        self._recent_commands.record(
            property,
            value,
            role=role,
            vfo=vfo,
        )

    def _handle_async_event(self, event: RadioStateEvent) -> None:
        matched = None
        if event.classification != RadioStateClassification.STATE_REFRESH:
            matched = self._recent_commands.match(event)
        if matched is not None:
            event = event.with_route(
                matched.role,
                classification=RadioStateClassification.SELF_ECHO,
            )
            roles = [matched.role] if matched.role else self._route_event_roles(event)
        else:
            roles = self._route_event_roles(event)

        roles = [role for role in roles if role is not None]
        if not roles:
            return
        ambiguous = len(roles) > 1
        for role in roles:
            routed = event.with_route(
                role,
                classification=(
                    RadioStateClassification.STATE_REFRESH
                    if ambiguous and matched is None
                    else event.classification
                ),
                requires_reconciliation=ambiguous and matched is None,
            )
            with self._event_lock:
                self._event_queues[role].append(routed)
                if (
                    routed.classification != RadioStateClassification.STATE_REFRESH
                    and routed.property in {
                        RadioStateProperty.FREQUENCY,
                        RadioStateProperty.MODE,
                    }
                    and not routed.requires_reconciliation
                ):
                    newly_verified = routed.property not in self._verified_properties[role]
                    self._verified_properties[role].add(routed.property)
                else:
                    newly_verified = False
            if (
                routed.property == RadioStateProperty.FREQUENCY
                and not routed.requires_reconciliation
            ):
                self._remember_role_frequency(role, routed.value)
            LOGGER.debug(
                "async state event property=%s role=%s vfo=%s value=%s classification=%s reconcile=%s",
                routed.property,
                routed.role,
                routed.vfo,
                routed.value,
                routed.classification,
                routed.requires_reconciliation,
            )
            if newly_verified:
                LOGGER.info(
                    "local_hamlib role=%s async_verified property=%s",
                    role,
                    routed.property.value,
                )
            if (
                routed.property == RadioStateProperty.FREQUENCY
                and routed.classification == RadioStateClassification.EXTERNAL_CHANGE
            ):
                LOGGER.info(
                    "local_hamlib role=%s async_external_change property=frequency "
                    "vfo=%s value=%s",
                    role,
                    routed.vfo,
                    routed.value,
                )

    def _route_event_roles(self, event: RadioStateEvent) -> list[str]:
        event_vfo = normalize_event_vfo(event.vfo)
        with self._event_lock:
            role_vfos = dict(self._role_vfos)
        exact = [role for role, vfo in role_vfos.items() if event_vfo and vfo == event_vfo]
        frequency_role = self._role_for_frequency_hint(event)
        if frequency_role is not None:
            if exact and frequency_role not in exact:
                LOGGER.debug(
                    "async state event frequency_route_overrode_vfo vfo=%s "
                    "value=%s vfo_role=%s frequency_role=%s",
                    event.vfo,
                    event.value,
                    exact,
                    frequency_role,
                )
            return [frequency_role]
        if exact:
            return exact
        if len(role_vfos) > 1 and event_vfo is not None:
            # Hamlib snapshots can contain overlapping aliases for the same
            # cached VFO (for example VFOA, Main, and MainA). The snapshot's
            # rx/tx flags describe Hamlib split/current-VFO state, not Pi-Sat's
            # configured logical RX/TX roles. Never use those flags to route a
            # named but unconfigured VFO on a shared radio.
            LOGGER.debug(
                "async state event ignored_unmapped_shared_vfo property=%s "
                "vfo=%s value=%s configured=%s",
                event.property,
                event.vfo,
                event.value,
                role_vfos,
            )
            return []
        event_role = _normalize_role(event.role)
        if event_role in role_vfos:
            return [event_role]
        if len(role_vfos) == 1:
            return list(role_vfos)
        return list(role_vfos)

    def _role_for_frequency_hint(self, event: RadioStateEvent) -> str | None:
        if (
            event.property != RadioStateProperty.FREQUENCY
            or event.classification == RadioStateClassification.STATE_REFRESH
        ):
            return None
        try:
            frequency_hz = int(event.value)
        except (TypeError, ValueError):
            return None
        with self._event_lock:
            if len(self._role_vfos) < 2:
                return None
            hints = dict(self._role_frequency_hints)
        if len(hints) < 2:
            return None
        ranked = sorted(
            (abs(frequency_hz - hint_hz), role)
            for role, hint_hz in hints.items()
        )
        nearest_distance, nearest_role = ranked[0]
        second_distance = ranked[1][0]
        if nearest_distance > ASYNC_ROLE_ROUTE_MAX_DISTANCE_HZ:
            return None
        if second_distance - nearest_distance < ASYNC_ROLE_ROUTE_MIN_ADVANTAGE_HZ:
            return None
        return nearest_role

    def _remember_role_frequency(self, role: str | None, value: object) -> None:
        if role is None:
            return
        try:
            frequency_hz = int(value)
        except (TypeError, ValueError):
            return
        if frequency_hz <= 0:
            return
        with self._event_lock:
            self._role_frequency_hints[role] = frequency_hz

    def _role_for_vfo(self, vfo: str | None) -> str | None:
        normalized_vfo = normalize_event_vfo(vfo)
        with self._event_lock:
            exact = [role for role, target in self._role_vfos.items() if target == normalized_vfo]
            if len(exact) == 1:
                return exact[0]
            if len(self._role_vfos) == 1:
                return next(iter(self._role_vfos))
        return _normalize_role(self.role_label)

    def mark_async_property_unverified(
        self,
        property: RadioStateProperty | str,
        *,
        reason: str,
        role: str | None = None,
    ) -> None:
        normalized_role = _normalize_role(role) or _normalize_role(self.role_label)
        try:
            normalized_property = RadioStateProperty(property)
        except ValueError:
            return
        if normalized_role is None:
            return
        with self._event_lock:
            was_verified = normalized_property in self._verified_properties[normalized_role]
            self._verified_properties[normalized_role].discard(normalized_property)
        if was_verified:
            LOGGER.warning(
                "local_hamlib role=%s async_health_lost property=%s reason=%s "
                "falling_back_to_polling",
                normalized_role,
                normalized_property.value,
                reason,
            )

    def _expire_recent_commands(self) -> None:
        expired = self._recent_commands.expire()
        if expired:
            LOGGER.debug(
                "local_hamlib role=%s expired_recent_commands count=%d",
                self.role_label,
                len(expired),
            )


def _find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _build_rigctld_command(
    *,
    model_id: int,
    serial_port: str,
    baud: int,
    tcp_port: int,
    async_port: int | None,
    vfo_mode: bool,
    debug_logging: bool,
) -> list[str]:
    command = [
        "rigctld",
        "-T",
        "127.0.0.1",
        "-m",
        str(model_id),
        "-r",
        serial_port,
        "-s",
        str(baud),
        "-t",
        str(tcp_port),
    ]
    if async_port is not None:
        command.extend(
            [
                "-C",
                (
                    "async=1,multicast_data_addr=127.0.0.1,"
                    f"multicast_data_port={async_port},poll_interval=0"
                ),
            ]
        )
    if vfo_mode:
        command.insert(1, "-o")
    if debug_logging:
        command.insert(1, "-vvvvv")
    return command


def _normalize_state_updates(value: str | None) -> str:
    return "polling" if str(value or "").strip().lower() == "polling" else "automatic"


def _normalize_role(value: str | None) -> str | None:
    role = str(value or "").strip().lower()
    return role if role in {"rx", "tx"} else None


def _drain_rigctld_logs(stream, role_label: str) -> None:
    for line in stream:
        LOGGER.info("rigctld_raw role=%s %s", role_label, line.rstrip())
