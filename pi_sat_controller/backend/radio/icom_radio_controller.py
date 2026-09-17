from __future__ import annotations

"""Transport-neutral native Icom controller for the IC-9700.

This is the sole owner of CI-V framing, command correlation, radio state,
Main/Sub semantics, tracking serialization, and application audio policy.
Physical connectivity is supplied by an opaque byte/PCM transport.
"""

from dataclasses import dataclass
from collections import deque
from copy import deepcopy
from contextlib import contextmanager
import logging
import struct
from threading import Event, Lock, RLock, Thread, current_thread
from time import monotonic, sleep
from typing import Any, Callable

from pi_sat_controller.backend.radio.audio_buffer import AudioBuffer
from pi_sat_controller.backend.radio.icom_connectivity import IcomConnectivity, IcomError
from pi_sat_controller.backend.radio.radio_state import (
    RadioStateEvent,
    RadioStateProperty,
    RecentCommandHistory,
)

LOGGER = logging.getLogger(__name__)

# IC-9700 DATA OFF MOD / DATA MOD input sources (CI-V 1A 05 01 15 and 16).
# Pi-Sat's own transmit audio only travels over the LAN input; the other
# sources are accepted so an operator can hand the radio to a different
# input without Pi-Sat going near the menu.
MICROPHONE_SOURCES: dict[str, int] = {
    "mic": 0,
    "acc": 1,
    "mic+acc": 2,
    "usb": 3,
    "mic+usb": 4,
    "lan": 5,
}


@dataclass(frozen=True)
class IcomRadioConfig:
    civ_address: int = 0xA2
    controller_address: int = 0xE0
    sample_rate: int = 16000
    rx_codec: str = "lpcm16_stereo"
    tx_codec: str = "lpcm16_mono"
    full_duplex: bool = True
    scope_enabled: bool = True
    debug_logging: bool = False


@dataclass
class IcomRadioState:
    connected: bool = False
    connecting: bool = False
    authenticated: bool = False
    physical_side: str = "MAIN"
    vfo: str | None = None
    main_vfo: str | None = None
    sub_vfo: str | None = None
    main_frequency_hz: int | None = None
    sub_frequency_hz: int | None = None
    main_filter: int | None = None
    sub_filter: int | None = None
    main_bandwidth_hz: int | None = None
    sub_bandwidth_hz: int | None = None
    main_af_gain: int | None = None
    sub_af_gain: int | None = None
    main_rf_gain: int | None = None
    sub_rf_gain: int | None = None
    main_squelch: int | None = None
    sub_squelch: int | None = None
    main_s_meter: int | None = None
    sub_s_meter: int | None = None
    main_squelch_open: bool | None = None
    sub_squelch_open: bool | None = None
    power_meter: int | None = None
    swr_meter: int | None = None
    compression_meter: int | None = None
    tx_meter_updated: float | None = None
    sub_rit_hz: int | None = None
    sub_rit_enabled: bool | None = None
    lan_mod_level: int | None = None
    data_off_mod: int | None = None
    data_mod: int | None = None
    dualwatch: bool | None = None
    main_updated: float | None = None
    sub_updated: float | None = None
    main_a_hz: int | None = None
    main_b_hz: int | None = None
    sub_a_hz: int | None = None
    sub_b_hz: int | None = None
    main_mode: str | None = None
    sub_mode: str | None = None
    ptt: bool | None = None
    # Commanded PTT state while it differs from the confirmed radio readback.
    # None means the last command has been confirmed (or nothing is pending).
    ptt_pending: bool | None = None
    # Set when Pi-Sat keyed the radio and then lost the ability to confirm a
    # release. Sticky until a confirmed idle readback, so the console can warn
    # that the radio may still be transmitting.
    tx_unconfirmed: bool = False
    rx_audio_packets: int = 0
    tx_audio_packets: int = 0
    scope_lines: int = 0
    last_error: str | None = None
    last_update_monotonic: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "connected": self.connected,
            "connecting": self.connecting,
            "authenticated": self.authenticated,
            "physical_side": self.physical_side,
            "vfo": self.vfo,
            **{side: {"a_hz": getattr(self, f"{side}_a_hz"), "b_hz": getattr(self, f"{side}_b_hz"),
                      "frequency_hz": getattr(self, f"{side}_frequency_hz"), "vfo": getattr(self, f"{side}_vfo"),
                      "mode": getattr(self, f"{side}_mode"), "filter": getattr(self, f"{side}_filter"),
                      **{key: getattr(self, f"{side}_{key}") for key in
                         ("bandwidth_hz", "af_gain", "rf_gain", "squelch", "s_meter", "squelch_open")},
                      "updated_monotonic": getattr(self, f"{side}_updated")}
               for side in ("main", "sub")},
            "sub_rit": {"offset_hz": self.sub_rit_hz, "enabled": self.sub_rit_enabled},
            "microphone": {"lan_mod_level": self.lan_mod_level, "data_off_mod": self.data_off_mod, "data_mod": self.data_mod},
            "dualwatch": self.dualwatch,
            "ptt": self.ptt,
            "ptt_pending": self.ptt_pending,
            "tx_unconfirmed": self.tx_unconfirmed,
            "tx_meters": {"power": self.power_meter, "swr": self.swr_meter, "compression": self.compression_meter,
                          "updated_monotonic": self.tx_meter_updated},
            "rx_audio_packets": self.rx_audio_packets,
            "tx_audio_packets": self.tx_audio_packets,
            "scope_lines": self.scope_lines,
            "last_error": self.last_error,
        }


class IcomRadioError(IcomError):
    """A CI-V command or readback failed while its transport stayed usable."""


class IcomRadioController:
    """Single semantic controller over one selected connectivity module."""

    # How long a PTT release keeps re-asserting the unkey while the radio still
    # reports transmitting. The LAN audio stream carries a jitter buffer the
    # radio drains after our last packet, so an immediate readback can honestly
    # still read "transmitting" even though the unkey was accepted.
    _PTT_RELEASE_GRACE_S = 2.0
    _PTT_RELEASE_SETTLE_S = 0.2
    # Attempts used by the best-effort unkey that runs before a teardown.
    _PTT_PANIC_ATTEMPTS = 2
    # Keying is confirmed with a bounded retry: a readback taken immediately
    # after the key command can still report receiving while the radio switches.
    _PTT_KEY_CONFIRM_S = 0.6
    _PTT_KEY_RETRY_S = 0.05
    # Release confirmation runs on the session loop, not on the caller.
    _PTT_CONFIRM_POLL_S = 0.05
    # Silence is fed into the transmit stream after this long without
    # microphone audio, so a stalled or disabled capture does not starve the
    # radio's transmit buffer while it is keyed.
    _TX_SILENCE_FILL_S = 0.06
    # Slow telemetry inventory cadence (levels, RIT, microphone settings).
    # Frequency/mode changes never shorten this; they use a targeted read.
    _RECONCILE_INTERVAL_S = 2.0
    # How often the side the radio is not parked on gets a full read.
    _INACTIVE_SIDE_INTERVAL_S = 10.0

    def __init__(self, config: IcomRadioConfig, connectivity: IcomConnectivity,
                 stop_event: Event | None = None) -> None:
        self.config = config
        self.connectivity = connectivity
        self._lock = RLock()
        self._audio_lock = Lock()
        self._scope_lock = Lock()
        self._snapshot_lock = Lock()
        self._operator_lock = Lock()
        self._lifecycle_lock = RLock()
        self._stop = stop_event or Event()
        self._thread: Thread | None = None
        self._state = IcomRadioState()
        self._audio_queue: deque[bytes] = deque(maxlen=64)
        self._scope_queue: deque[bytes] = deque(maxlen=32)
        self._scope_divisions: dict[int, tuple[bytes, int, bytearray]] = {}
        self._browser_audio_seq = 0
        self._stage = "idle"
        self._pending_civ: bytes | None = None
        self._civ_response: bytes | None = None
        self._serial_buffer = bytearray()
        self._last_reconcile = 0.0
        self._latest_scope: bytes | None = None
        self._audio_subscribers: list[AudioBuffer] = []
        self._operator_waiters = 0
        self._connection_generation = 0
        self._connection_attempt = 0
        self._retry_in_s: float | None = None
        self._ptt_confirm_due = 0.0
        self._ptt_idle_since: float | None = None
        self._ptt_release_deadline: float | None = None
        self._last_tx_audio_at: float | None = None
        # Whether Pi-Sat has commanded a key and not yet commanded a release.
        # Transmit audio is gated on this, not on the radio's readback, which
        # can lag or flicker while the transmitter is running.
        self._ptt_commanded = False
        # Set by a caller that feeds its own real-time paced stream (the APRS
        # modulator). Silence fill must never insert frames into that stream.
        self._transmit_paced = False
        # --- Transport diagnostics: bounded counters, never per-packet logging.
        self._civ_counts: dict[str, int] = {}
        self._transceive_counts: dict[str, int] = {}
        self._select_count = 0
        self._select_threads: dict[str, int] = {}
        self._inventory_runs = 0
        self._targeted_refresh_runs = 0
        self._lock_wait_max_s = 0.0
        self._lock_hold_max_s = 0.0
        self._tx_audio_write_max_s = 0.0
        # --- Transceive handling. The radio pushes untagged timestamps 0x00
        # (frequency) and 0x01 (mode); the shared command history tells us when
        # one is the echo of a write we just made so it costs nothing to apply.
        self._recent_commands = RecentCommandHistory()
        self._confirmed_echoes: deque[tuple[str, object, float]] = deque(maxlen=32)
        self._external_refresh_due: float | None = None
        self._external_refresh_properties: set[str] = set()
        self._tx_meter_due = 0.0
        # Per-side scope delivery counters. The radio may send one CI-V frame
        # per sweep or split a sweep into many "division" frames; the two look
        # wildly different on screen, so count them before blaming the wire.
        self._scope_counts: dict[str, dict[str, int]] = {}
        # The audio lock is a leaf: it serializes transport PCM buffering only.
        # The transmit-audio path never takes the CI-V controller lock, so a
        # microphone frame cannot queue behind tracking or telemetry. The epoch
        # invalidates frames that were already in flight when a release landed.
        self._tx_audio_lock = Lock()
        self._tx_audio_epoch = 0
        # The inactive side is refreshed occasionally instead of every cycle so
        # telemetry stops toggling the radio's MAIN/SUB selection.
        self._inactive_side_due = 0.0
        # Built last: the snapshot reads every counter above.
        self._snapshot_cache = self._snapshot_payload_locked()

    @property
    def state(self) -> IcomRadioState:
        with self._lock:
            return IcomRadioState(**self._state.__dict__)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return self._publish_snapshot_locked()

    def _snapshot_payload_locked(self) -> dict[str, Any]:
        payload = self._state.to_dict()
        now = monotonic()
        for side in ("main", "sub"):
            updated = getattr(self._state, f"{side}_updated")
            payload[side]["age_s"] = int(now - updated) if updated is not None else None
        payload["tx_meters"]["age_s"] = int(now - self._state.tx_meter_updated) if self._state.tx_meter_updated is not None else None
        payload.update({"sample_rate": self.config.sample_rate, "rx_codec": self.config.rx_codec, "tx_codec": "lpcm16_mono",
                        "rx_channels": 2 if self.config.rx_codec == "lpcm16_stereo" else 1,
                        "connection_stage": self._stage,
                        "connection_attempt": self._connection_attempt,
                        "retry_in_s": self._retry_in_s})
        payload.update(self.connectivity.snapshot())
        payload["transport_diagnostics"] = self._transport_diagnostics_locked()
        return payload

    def _publish_snapshot_locked(self) -> dict[str, Any]:
        payload = self._snapshot_payload_locked()
        with self._snapshot_lock:
            self._snapshot_cache = payload
        return deepcopy(payload)

    def try_snapshot(self) -> dict[str, Any]:
        """Return fresh state when possible, otherwise the last complete snapshot."""
        if not self._lock.acquire(blocking=False):
            with self._snapshot_lock:
                return deepcopy(self._snapshot_cache)
        try:
            return self._publish_snapshot_locked()
        finally:
            self._lock.release()

    def connection_generation(self) -> int:
        with self._lock:
            return self._connection_generation

    @contextmanager
    def operation_batch(self):
        """Serialize a complete tracking cycle with Radio-page commands."""

        with self._operator_operation():
            yield

    @contextmanager
    def _operator_operation(self):
        """Give live control work priority over background radio auditing.

        A caller announces itself before waiting for the CI-V lock.  The
        background audit checks this flag between small readback groups and
        yields instead of making a button wait behind the complete inventory.
        """

        wait_started = monotonic()
        with self._operator_lock:
            self._operator_waiters += 1
        try:
            with self._lock:
                acquired_at = monotonic()
                self._lock_wait_max_s = max(self._lock_wait_max_s, acquired_at - wait_started)
                try:
                    yield
                finally:
                    self._lock_hold_max_s = max(self._lock_hold_max_s, monotonic() - acquired_at)
        finally:
            with self._operator_lock:
                self._operator_waiters -= 1

    def _operator_is_waiting(self) -> bool:
        with self._operator_lock:
            return self._operator_waiters > 0

    def start(self) -> dict[str, Any]:
        with self._lifecycle_lock, self._lock:
            if self._thread and self._thread.is_alive():
                return self.snapshot()
            self._stop.clear()
            self._state.connecting = True
            self._state.last_error = None
            self._connection_attempt = 0
            self._retry_in_s = None
            self._thread = Thread(target=self._run, name="icom-radio-controller", daemon=True)
            self._thread.start()
            # Return while we still own the lock, before the worker enters the
            # handshake. HTTP callers can then cancel a connecting session.
            return self.snapshot()

    def stop(self) -> None:
        with self._lifecycle_lock:
            # Unkey before the session is torn down: once _stop is set no CI-V
            # command can be delivered, and leaving the radio transmitting is
            # the one failure mode the operator cannot recover remotely.
            with self._lock:
                self._release_ptt_best_effort("shutdown")
            self._stop.set()
            if self._thread and self._thread.is_alive():
                self._thread.join(timeout=3.0)
                if self._thread.is_alive():
                    raise IcomRadioError("Radio worker is still stopping; reconnect is not yet available")
            with self._lock:
                self.connectivity.disconnect()
                self._clear_disconnected_state_locked()

    def connect(self) -> dict[str, Any]:
        with self._lock:
            try:
                if not self._state.connected:
                    self._stage = "connecting"
                    self.connectivity.connect()
                    self._state.connected = True
                    self._state.tx_unconfirmed = False
                    self._state.authenticated = bool(
                        self.connectivity.snapshot().get("transport_authenticated", True)
                    )
                    self._connection_generation += 1
                    self._stage = "connected"
                    self._bump_tx_audio_epoch_locked()
                    try:
                        self._prime_frequency_state_locked()
                        self._last_reconcile = monotonic()
                    except Exception as exc:
                        LOGGER.info("Icom %s connected; initial frequency read failed: %s",
                                    self.connectivity.kind, exc)
                    self._activate_transport_microphone_locked()
            except Exception:
                self.connectivity.disconnect()
                self._clear_disconnected_state_locked()
                raise
            return self._state.to_dict()

    def disconnect(self) -> None:
        restore_error: Exception | None = None
        with self._lifecycle_lock:
            try:
                with self._operator_operation():
                    if self._state.connected and not self._stop.is_set():
                        # The hand microphone can only be restored while the
                        # radio is receiving, so unkey before reconfiguring it.
                        self._release_ptt_best_effort("disconnect")
                        self._configure_microphone_locked(source=0)
            except Exception as exc:
                restore_error = exc
                LOGGER.warning("Could not restore the IC-9700 hand microphone before disconnect: %s", exc)
            self.stop()
        if restore_error is not None:
            raise IcomRadioError(
                f"Radio disconnected, but the hand microphone could not be restored: {restore_error}"
            ) from restore_error

    def get_frequency(self, physical_side: str) -> int:
        """Read the side's active VFO immediately before tracking reconciliation."""
        side = self._normalize_side(physical_side)
        with self._operator_operation():
            self._ensure_connected_locked()
            previous_side = self._read_side_locked()
            try:
                self._select_locked(side, None)
                frequency = self._decode_frequency_response(
                    self._civ_transaction_locked(self._frame(0x03))
                )
                if frequency is None:
                    raise IcomRadioError("Radio frequency readback is unavailable")
                if self._read_side_locked() != side:
                    raise IcomRadioError("MAIN/SUB changed during frequency readback")
                self._set_frequency_state(side, getattr(self._state, f"{side.lower()}_vfo"), frequency)
                return frequency
            finally:
                self._select_locked(previous_side, None)

    def set_frequency(self, physical_side: str, vfo: str | None, frequency_hz: int) -> dict[str, Any]:
        physical_side = self._normalize_side(physical_side)
        vfo = self._normalize_vfo(vfo) if vfo is not None else None
        if type(frequency_hz) is not int or not 0 < frequency_hz < 10_000_000_000:
            raise ValueError("frequency_hz must be a positive integer in the five-byte CI-V range")
        with self._operator_operation():
            self._ensure_connected_locked()
            self._read_side_locked()
            previous_side, previous_vfo = self._state.physical_side, self._state.vfo
            try:
                self._select_locked(physical_side, vfo)
                # Record the echo we expect before the write so the radio's own
                # transceive frame is applied for free instead of arming a
                # reconciliation read.
                self._recent_commands.record(
                    RadioStateProperty.FREQUENCY, frequency_hz,
                    vfo=self._echo_vfo(physical_side, vfo),
                )
                self._civ_transaction_locked(self._frame(0x05, self._bcd_frequency(frequency_hz)))
                actual = self._read_frequency_state_locked(physical_side)
                if actual != frequency_hz:
                    raise IcomRadioError("Radio frequency readback did not match the requested frequency")
            except Exception:
                # Leave the recorded echo to expire on its own: it names the
                # value we tried to reach, so a later match is still harmless.
                raise
            finally:
                self._select_locked(previous_side, previous_vfo if vfo is not None else None)
            return self._state.to_dict()

    def set_mode(self, physical_side: str, mode: str, vfo: str | None = None) -> dict[str, Any]:
        physical_side = self._normalize_side(physical_side)
        mode_code = {"LSB": 0x00, "USB": 0x01, "AM": 0x02, "CW": 0x03, "FM": 0x05,
                     "CW-R": 0x07, "DV": 0x17, "DD": 0x22}.get(mode.upper())
        if mode_code is None:
            raise ValueError(f"Unsupported IC-9700 mode: {mode}")
        with self._operator_operation():
            self._ensure_connected_locked()
            self._read_side_locked()
            previous_side, previous_vfo = self._state.physical_side, self._state.vfo
            try:
                self._select_locked(physical_side, self._normalize_vfo(vfo) if vfo is not None else None)
                # Let the radio choose the mode's default filter, then read it back.
                self._recent_commands.record(
                    RadioStateProperty.MODE, mode_code,
                    vfo=self._echo_vfo(physical_side, self._state.vfo),
                )
                self._civ_transaction_locked(self._frame(0x06, bytes([mode_code])))
                actual, _ = self._read_mode_state_locked(physical_side)
                if actual != mode.upper():
                    raise IcomRadioError("Radio mode readback did not match the requested mode")
            except Exception:
                # As above: let the unused echo expire rather than clearing the
                # whole history and losing unrelated pending echoes.
                raise
            finally:
                self._select_locked(previous_side, previous_vfo)
            return self._state.to_dict()

    def select(self, physical_side: str, vfo: str) -> dict[str, Any]:
        with self._operator_operation():
            self._ensure_connected_locked()
            self._select_locked(self._normalize_side(physical_side), self._normalize_vfo(vfo))
            self._read_current_locked()
            return self._state.to_dict()

    def set_filter(self, physical_side: str, filter_number: int) -> dict[str, Any]:
        side = self._normalize_side(physical_side)
        if type(filter_number) is not int or filter_number not in (1, 2, 3):
            raise ValueError("Filter must be FIL1, FIL2 or FIL3")
        with self._operator_operation():
            self._ensure_connected_locked()
            previous_side = self._read_side_locked()
            try:
                self._select_locked(side, None)
                mode = self._civ_transaction_locked(self._frame(0x04))
                if len(mode) not in (7, 8):
                    raise IcomRadioError("Invalid mode readback; filter write cancelled")
                self._civ_transaction_locked(self._frame(0x06, bytes([mode[5], filter_number])))
                _, actual = self._read_mode_state_locked(side)
                if actual != filter_number:
                    raise IcomRadioError("Radio filter readback did not match the requested filter")
            finally:
                self._select_locked(previous_side, None)
            return self._state.to_dict()

    def set_ptt(self, enabled: bool) -> dict[str, Any]:
        if type(enabled) is not bool:
            raise ValueError("PTT enabled must be a boolean")
        with self._operator_operation():
            self._ensure_connected_locked()
            self._write_ptt_locked(enabled)
            if enabled:
                # Transmit audio is gated on the commanded key, not on the
                # radio's readback, which can flicker while transmitting.
                self._ptt_commanded = True
                self._confirm_ptt_key_locked()
            else:
                self._begin_ptt_release_locked()
            return self._state.to_dict()

    def _write_ptt_locked(self, enabled: bool) -> None:
        # IC-9700 PTT/status command is kept in the model table rather than
        # borrowing a generic Icom command from another radio family.
        self._civ_transaction_locked(self._frame(0x1C, bytes([0x00, 0x01 if enabled else 0x00])))

    def _confirm_ptt_key_locked(self) -> None:
        """Confirm a key, retrying a lagging readback instead of failing it.

        The radio can still report receiving for a moment after the key
        command. Treating that first read as a hard failure aborted voice and
        APRS transmissions, so retry inside a bounded window.
        """
        deadline = monotonic() + self._PTT_KEY_CONFIRM_S
        while True:
            if self._read_ptt_state_locked() is True:
                self._state.tx_unconfirmed = False
                self._state.ptt_pending = None
                self._ptt_release_deadline = None
                self._prime_transmit_audio_locked()
                return
            if self._stop.is_set() or monotonic() >= deadline:
                # The key command itself was accepted, so the radio may be
                # transmitting even though the readback never matched. Never
                # report a failed key and leave it keyed.
                self._best_effort_unkey_command("unconfirmed key")
                self._ptt_commanded = False
                self._bump_tx_audio_epoch_locked()
                raise IcomRadioError("Radio PTT readback did not match the request")
            self._hold_locked(min(self._PTT_KEY_RETRY_S, max(0.0, deadline - monotonic())))

    def _begin_ptt_release_locked(self) -> None:
        """Mark a commanded release for the session loop to confirm.

        Confirmation must not block the caller. The radio legitimately keeps
        reporting transmitting while its transmit audio drains, and holding the
        API thread and the radio lock for that window is what made PTT feel
        laggy. The unkey command itself has already been accepted by the time
        this runs, so the session loop only has to observe the radio settle.
        """
        self._state.ptt_pending = False
        self._ptt_commanded = False
        self._transmit_paced = False
        # A release invalidates any microphone frame still in flight, so stale
        # PCM cannot be carried into the next transmission.
        self._bump_tx_audio_epoch_locked()
        self._ptt_idle_since = None
        self._ptt_release_deadline = monotonic() + self._PTT_RELEASE_GRACE_S
        self._ptt_confirm_due = 0.0
        # Drop audio the operator queued before releasing: carrying a partial
        # frame into the next transmission would prepend it with stale samples.
        self._discard_transmit_audio()
        self._last_tx_audio_at = None

    def _service_ptt_confirmation_locked(self, now: float) -> None:
        """Advance a pending PTT command. Runs on the transport session loop."""
        pending = self._state.ptt_pending
        if pending is None or self._stop.is_set() or not self._state.connected:
            return
        if now < self._ptt_confirm_due:
            return
        self._ptt_confirm_due = now + self._PTT_CONFIRM_POLL_S
        try:
            confirmed = self._read_ptt_state_locked()
        except IcomError as exc:
            # A failed confirmation read is not a dead link. The transport
            # session loop is the authority on link health, so a transient
            # error here must not end the session mid-transmission.
            self._state.last_error = str(exc)
            LOGGER.info("PTT confirmation read failed (%s); the session stays up", type(exc).__name__)
            return
        if confirmed is pending:
            # A single matching read is not proof: CI-V replies carry no request
            # identity, so a stale or duplicated frame can land in the slot. Hold
            # the state until it still reads the same after the settle window.
            if self._ptt_idle_since is None:
                self._ptt_idle_since = now
                return
            if now - self._ptt_idle_since >= self._PTT_RELEASE_SETTLE_S:
                self._state.ptt_pending = None
                self._ptt_idle_since = None
                self._ptt_release_deadline = None
                self._state.tx_unconfirmed = False
            return
        self._ptt_idle_since = None
        if now >= (self._ptt_release_deadline or 0.0):
            self._state.ptt_pending = None
            self._ptt_release_deadline = None
            self._state.tx_unconfirmed = True
            LOGGER.critical("PTT release was not confirmed within %.1fs; the radio may still be transmitting",
                            self._PTT_RELEASE_GRACE_S)
            return
        try:
            self._write_ptt_locked(False)
        except IcomRadioError as exc:
            LOGGER.debug("PTT release retry could not re-assert the unkey: %s", exc)

    def wait_for_ptt(self, expected: bool, timeout_s: float) -> bool:
        """Block until the radio confirms ``expected`` PTT, or the deadline passes."""
        deadline = monotonic() + max(0.0, timeout_s)
        while True:
            with self._lock:
                if self._state.ptt is expected:
                    return True
                if not self._state.connected or self._stop.is_set():
                    return False
                self._service_ptt_confirmation_locked(monotonic())
            if monotonic() >= deadline:
                return False
            sleep(0.02)

    def release_ptt_if_audio_source_lost(self) -> None:
        """Release PTT when the session that carried the microphone ended.

        The microphone only reaches the radio through that session, so leaving
        the radio keyed would transmit a bare carrier with nothing on it.
        """
        with self._lock:
            if not self._ptt_commanded:
                return
        try:
            self.set_ptt(False)
        except Exception as exc:
            LOGGER.warning("Could not release PTT after the audio session ended: %s", exc)
            with self._lock:
                self._state.tx_unconfirmed = True

    def _read_ptt_state_locked(self) -> bool:
        response = self._civ_transaction_locked(self._frame(0x1C, b"\x00"))
        if len(response) != 8 or response[6] not in (0, 1):
            raise IcomRadioError("Invalid PTT readback")
        self._state.ptt = bool(response[6])
        return self._state.ptt

    def _release_ptt_best_effort(self, reason: str) -> bool:
        """Command an unkey before a teardown without waiting for confirmation.

        Best effort by construction: if the link is gone there is nothing left
        to send. The only honest outcome then is to flag that the radio may
        still be transmitting so the operator can confirm it locally.
        """
        # Keyed means Pi-Sat commanded a key, even if the readback never
        # confirmed it: that is exactly the case where the radio may be stuck on.
        if not self._ptt_commanded:
            return True
        if not self._state.connected:
            self._state.tx_unconfirmed = True
            LOGGER.critical("Radio may still be transmitting after %s: no link to send an unkey", reason)
            return False
        if self._best_effort_unkey_command(reason):
            # Sent but unconfirmed: unknown is more honest than "receiving".
            self._state.ptt = None
            LOGGER.warning("Unkey sent before %s; release is unconfirmed", reason)
            return True
        self._state.tx_unconfirmed = True
        LOGGER.critical("Radio may still be transmitting after %s: unkey was not accepted", reason)
        return False

    def _best_effort_unkey_command(self, reason: str) -> bool:
        """Send the unkey command itself, with no precondition on cached state."""
        for attempt in range(self._PTT_PANIC_ATTEMPTS):
            try:
                self._write_ptt_locked(False)
            except Exception as exc:
                LOGGER.warning("Unkey before %s failed on attempt %d of %d: %s",
                               reason, attempt + 1, self._PTT_PANIC_ATTEMPTS, exc)
                if attempt + 1 >= self._PTT_PANIC_ATTEMPTS:
                    break
                sleep(0.05)
                continue
            return True
        return False

    def _hold_locked(self, seconds: float) -> None:
        """Wait for the radio while servicing the session.

        The LAN stream needs its keepalive, token renewal and audio drain to
        keep running, so the wait is spent polling the transport rather than
        sleeping on the CI-V lock.
        """
        hold_until = monotonic() + max(0.0, seconds)
        while not self._stop.is_set() and monotonic() < hold_until:
            self._service_once(min(0.05, hold_until - monotonic()))

    def set_dualwatch(self, enabled: bool) -> dict[str, Any]:
        if type(enabled) is not bool:
            raise ValueError("Dualwatch enabled must be a boolean")
        with self._operator_operation():
            self._ensure_connected_locked()
            self._civ_transaction_locked(self._frame(0x16, bytes([0x59, int(enabled)])))
            self._read_dualwatch_locked()
            if self._state.dualwatch != enabled:
                raise IcomRadioError("Dualwatch readback did not match the request")
            return self._state.to_dict()

    def _read_dualwatch_locked(self) -> None:
        response = self._civ_transaction_locked(self._frame(0x16, b"\x59"))
        if len(response) != 8 or response[6] not in (0, 1):
            raise IcomRadioError("Invalid Dualwatch readback")
        self._state.dualwatch = bool(response[6])

    def configure_microphone(self, use_lan: bool = False, lan_mod_level: int | None = None,
                             use_transport: bool = False,
                             source: str | int | None = None) -> dict[str, Any]:
        if type(use_lan) is not bool or type(use_transport) is not bool or (lan_mod_level is not None and
                (type(lan_mod_level) is not int or not 0 <= lan_mod_level <= 255)):
            raise ValueError("LAN modulation level must be an integer from 0 to 255")
        # An explicit source sets both DATA OFF MOD and DATA MOD together. The
        # boolean flags remain for the connect path and existing callers.
        resolved_source = (self._resolve_microphone_source(source) if source is not None
                           else 5 if (use_lan or use_transport) else None)
        with self._operator_operation():
            self._ensure_connected_locked()
            self._configure_microphone_locked(source=resolved_source, lan_mod_level=lan_mod_level)
            return self._state.to_dict()

    def _activate_transport_microphone_locked(self) -> None:
        self._configure_microphone_locked(source=5)

    def _configure_microphone_locked(self, *, source: int | None = None,
                                     lan_mod_level: int | None = None) -> None:
        if source is not None or lan_mod_level is not None:
            ptt = self._civ_transaction_locked(self._frame(0x1C, b"\x00"))
            if len(ptt) != 8 or ptt[6] != 0:
                raise IcomRadioError("Release PTT before changing microphone configuration")
        self._read_microphone_config_locked()
        source_change = source is not None and (
            self._state.data_off_mod != source or self._state.data_mod != source
        )
        level_change = lan_mod_level is not None and self._state.lan_mod_level != lan_mod_level
        if source_change:
            for setting in (0x15, 0x16):
                self._civ_transaction_locked(self._frame(0x1A, bytes([5, 1, setting, source])))
        if level_change:
            self._civ_transaction_locked(
                self._frame(0x1A, b"\x05\x01\x14" + bytes.fromhex(f"{lan_mod_level:04d}"))
            )
        if source_change or level_change:
            self._read_microphone_config_locked()
        if ((source is not None and (self._state.data_off_mod != source or self._state.data_mod != source))
                or (lan_mod_level is not None and self._state.lan_mod_level != lan_mod_level)):
            raise IcomRadioError("Microphone configuration readback did not match the request")

    def _read_microphone_config_locked(self) -> None:
        values = {}
        for key, setting in (("lan_mod_level", 0x14), ("data_off_mod", 0x15), ("data_mod", 0x16)):
            response = self._civ_transaction_locked(self._frame(0x1A, bytes([5, 1, setting])))
            if setting == 0x14:
                # The level has two BCD bytes after the three-byte setting ID.
                if len(response) != 11:
                    raise IcomRadioError("Invalid LAN modulation level readback")
                values[key] = self._decode_level(response[:6] + response[8:])
            else:
                if len(response) != 10 or response[8] not in range(6):
                    raise IcomRadioError("Invalid microphone source readback")
                values[key] = response[8]
        for key, value in values.items():
            setattr(self._state, key, value)

    def set_level(self, physical_side: str, control: str, value: int) -> dict[str, Any]:
        side = self._normalize_side(physical_side)
        subcommand = {"af_gain": 1, "rf_gain": 2, "squelch": 3}.get(control)
        if subcommand is None or type(value) is not int or not 0 <= value <= 255:
            raise ValueError("Level must be af_gain, rf_gain or squelch with an integer from 0 to 255")
        with self._operator_operation():
            self._ensure_connected_locked()
            previous_side = self._read_side_locked()
            try:
                self._select_locked(side, None)
                self._civ_transaction_locked(self._frame(0x14, bytes([subcommand]) + bytes.fromhex(f"{value:04d}")))
                response = self._civ_transaction_locked(self._frame(0x14, bytes([subcommand])))
                actual = self._decode_level(response)
                if self._read_side_locked() != side:
                    raise IcomRadioError("MAIN/SUB changed during level readback")
                setattr(self._state, f"{side.lower()}_{control}", actual)
                if actual != value:
                    raise IcomRadioError("Radio level readback did not match the requested value")
            finally:
                self._select_locked(previous_side, None)
            return self._state.to_dict()

    def tune(self, physical_side: str, delta_hz: int, expected_frequency_hz: int | None = None) -> dict[str, Any]:
        """Relative tuning of the explicitly selected physical path's current VFO."""
        side = self._normalize_side(physical_side)
        if type(delta_hz) is not int:
            raise ValueError("Tuning delta must be an integer number of Hz")
        with self._operator_operation():
            self._ensure_connected_locked()
            previous_side = self._read_side_locked()
            try:
                self._select_locked(side, None)
                current = self._decode_frequency_response(self._civ_transaction_locked(self._frame(3)))
                if current is None or (expected_frequency_hz is not None and current != expected_frequency_hz):
                    raise IcomRadioError("Frequency changed during gesture; tuning cancelled. Try again with current readback.")
                frequency = current + delta_hz
                encoded = self._bcd_frequency(frequency)
                if self._read_side_locked() != side:
                    raise IcomRadioError("MAIN/SUB changed; tuning cancelled")
                self._recent_commands.record(
                    RadioStateProperty.FREQUENCY, frequency,
                    vfo=self._echo_vfo(side, getattr(self._state, f"{side.lower()}_vfo")),
                )
                self._civ_transaction_locked(self._frame(5, encoded))
                actual = self._read_frequency_state_locked(side)
                if actual != frequency:
                    raise IcomRadioError("Radio frequency readback did not match tuning request")
            finally:
                self._select_locked(previous_side, None)
            return self._state.to_dict()

    def equalize_vfos(self, physical_side: str) -> dict[str, Any]:
        side = self._normalize_side(physical_side)
        with self._operator_operation():
            self._ensure_connected_locked()
            previous_side = self._read_side_locked()
            try:
                self._select_locked(side, None)
                self._civ_transaction_locked(self._frame(0x07, b"\xa0"))
                # Do not switch to the other VFO solely to verify the copy.
                # Invalidate its cached frequency; the ACK alone is not readback.
                setattr(self._state, f"{side.lower()}_a_hz", None)
                setattr(self._state, f"{side.lower()}_b_hz", None)
                self._read_current_locked()
            finally:
                self._select_locked(previous_side, None)
            return self._state.to_dict()

    def set_sub_rit(self, offset_hz: int | None = None, enabled: bool | None = None) -> dict[str, Any]:
        if offset_hz is not None and (type(offset_hz) is not int or abs(offset_hz) > 9990 or offset_hz % 10):
            raise ValueError("SUB RIT must be -9990 to 9990 Hz in 10 Hz steps")
        if enabled is not None and type(enabled) is not bool:
            raise ValueError("RIT enabled must be a boolean")
        with self._operator_operation():
            self._ensure_connected_locked()
            previous_side = self._read_side_locked()
            try:
                self._select_locked("SUB", None)
                if offset_hz is not None:
                    encoded = bytes.fromhex(f"{abs(offset_hz):04d}")[::-1] + bytes([int(offset_hz < 0)])
                    self._civ_transaction_locked(self._frame(0x21, b"\x00" + encoded))
                if enabled is not None:
                    self._civ_transaction_locked(self._frame(0x21, bytes([1, int(enabled)])))
                self._read_sub_rit_locked()
                if ((offset_hz is not None and self._state.sub_rit_hz != offset_hz)
                        or (enabled is not None and self._state.sub_rit_enabled != enabled)):
                    raise IcomRadioError("SUB RIT readback did not match request")
            finally:
                self._select_locked(previous_side, None)
            return self._state.to_dict()

    def _read_sub_rit_locked(self) -> None:
        if self._read_side_locked() != "SUB":
            raise IcomRadioError("RIT query requires SUB selection")
        offset = self._civ_transaction_locked(self._frame(0x21, b"\x00"))
        enabled = self._civ_transaction_locked(self._frame(0x21, b"\x01"))
        if (len(offset) != 10 or offset[8] not in (0, 1)
                or any(byte >> 4 > 9 or byte & 15 > 9 for byte in offset[6:8])
                or len(enabled) != 8 or enabled[6] not in (0, 1)):
            raise IcomRadioError("Invalid SUB RIT readback")
        if self._read_side_locked() != "SUB":
            raise IcomRadioError("MAIN/SUB changed during RIT readback")
        self._state.sub_rit_hz = int(offset[6:8][::-1].hex()) * (-1 if offset[8] else 1)
        self._state.sub_rit_enabled = bool(enabled[6])

    def drain_audio_packets(self) -> list[bytes]:
        with self._audio_lock:
            packets = list(self._audio_queue)
            self._audio_queue.clear()
            return packets

    def subscribe_audio(self, notify: Callable[[], None] | None = None) -> AudioBuffer:
        with self._audio_lock:
            queue = AudioBuffer(self.config.sample_rate, 2 if self.config.rx_codec == "lpcm16_stereo" else 1, notify)
            self._audio_subscribers.append(queue)
            return queue

    def unsubscribe_audio(self, queue: AudioBuffer) -> None:
        with self._audio_lock:
            self._audio_subscribers = [item for item in self._audio_subscribers if item is not queue]

    def read_audio(self, queue: AudioBuffer) -> list[bytes]:
        return queue.drain()

    def drain_scope_packets(self) -> list[bytes]:
        with self._scope_lock:
            packets = list(self._scope_queue)
            self._scope_queue.clear()
            return packets

    def scope_snapshot(self) -> dict[str, Any]:
        with self._scope_lock:
            return {"sequence": self._state.scope_lines,
                    "packet": self._latest_scope.hex() if self._latest_scope else None}

    def send_audio(self, pcm: bytes) -> None:
        """Feed a transmit stream this server paces itself (the APRS modulator)."""
        self._send_transmit_audio(pcm, browser_source=False)

    def send_microphone_audio(self, pcm: bytes) -> None:
        """Transmit browser microphone PCM.

        The browser streams microphone capture for as long as it holds the
        session, so a commanded key is deliberately not required to accept a
        frame here: the keyed check below is what keeps transmit audio off the
        radio while it is receiving. Unlike :meth:`send_audio` this refuses while
        a caller paces its own stream, so an APRS burst can never pick up
        microphone samples.
        """
        self._send_transmit_audio(pcm, browser_source=True)

    def _send_transmit_audio(self, pcm: bytes, *, browser_source: bool) -> None:
        """Hand PCM to the transport without taking the CI-V controller lock.

        The commanded key, the paced flag and the connection flag are single
        attribute assignments made under the controller lock, so they are read
        here directly rather than by taking that lock. Taking it would make
        microphone audio wait behind tracking and telemetry.
        """
        if not pcm:
            return
        if len(pcm) % 2:
            raise ValueError("Microphone PCM must contain complete 16-bit samples")
        if self.config.tx_codec not in ("lpcm16_mono", "lpcm16_stereo"):
            raise IcomRadioError("Only LPCM 16-bit mono microphone TX is supported")
        if not self._state.connected:
            raise IcomRadioError("Icom audio session is not connected")
        if not self._ptt_commanded:
            # Nothing is keyed, so transmit PCM has nowhere to go. A browser that
            # streams continuously depends on this being a quiet no-op.
            return
        if browser_source and self._transmit_paced:
            # A caller that paces its own transmit stream owns the radio's
            # transmit buffer for the duration, so browser samples are never
            # mixed into a modulated burst.
            return
        epoch = self._tx_audio_epoch
        with self._tx_audio_lock:
            # A release may have landed while this frame waited for the send
            # lock; stale PCM must never reach the next transmission.
            if epoch != self._tx_audio_epoch or not self._state.connected:
                return
            started_at = monotonic()
            packets = self.connectivity.write_audio(pcm)
            self._last_tx_audio_at = monotonic()
        self._state.tx_audio_packets += packets
        self._tx_audio_write_max_s = max(
            self._tx_audio_write_max_s, self._last_tx_audio_at - started_at
        )

    def _transmit_is_active(self) -> bool:
        """Whether Pi-Sat has commanded a key, whatever the radio reports.

        This decides whether a keyed transmitter still needs silent frames to
        keep its transmit buffer fed, and whether a paced stream owns the buffer.

        The decision uses the commanded key, not the radio's readback: a lagging
        or flickering PTT readback must not truncate a transmission that is
        actually keyed. The key is confirmed by readback before this is set, and
        a commanded release clears it.
        """
        return self._ptt_commanded

    def set_transmit_stream_paced(self, paced: bool) -> None:
        """Tell the core that this caller feeds its own paced transmit stream.

        The silence fill exists to keep the radio's transmit buffer fed when the
        browser's capture stream stalls. A caller that paces its own stream (the
        APRS modulator) must never have extra frames inserted into it: inserted
        audio corrupts the modulation and the frame will not decode.
        """
        with self._lock:
            self._transmit_paced = bool(paced)

    def _transmit_frame_bytes(self) -> int:
        """Bytes in one 20 ms mono LPCM16 transmit frame at the configured rate."""
        return max(2, int(self.config.sample_rate / 50) * 2)

    def _prime_transmit_audio_locked(self) -> None:
        """Feed one silent frame when a transmission starts.

        Feed a silent frame as soon as PTT is keyed so the radio's transmit
        buffer does not begin empty and delay the first syllable.
        """
        self._feed_transmit_silence(monotonic())

    def _transmit_silence_due_locked(self, now: float) -> bool:
        """Whether a keyed transmitter needs a silent frame to stay fed.

        The browser supplies a frame every capture period while it is running,
        so this only reports real gaps: capture disabled, a stalled client, or a
        dropped batch. Without a fill the radio's transmit buffer drains and the
        transmission goes silent.
        """
        if not self._transmit_is_active():
            return False
        if self._transmit_paced:
            return False
        last = self._last_tx_audio_at
        return last is None or now - last >= self._TX_SILENCE_FILL_S

    def _feed_transmit_silence(self, now: float) -> None:
        """Write one silent 20 ms frame under the session lock that owns the
        transmit frame buffer."""
        pcm = bytes(self._transmit_frame_bytes())
        try:
            # The audio lock is the leaf that serializes transport PCM buffering,
            # so the fill can never interleave with a microphone frame.
            with self._tx_audio_lock:
                self.connectivity.write_audio(pcm)
            self._last_tx_audio_at = now
        except Exception as exc:
            LOGGER.debug("Could not keep the transmit stream fed: %s", exc)

    def _discard_transmit_audio(self) -> None:
        """Drop TX PCM buffered for the radio when a transmission ends."""
        discard = getattr(self.connectivity, "discard_audio", None)
        if discard is None:
            return
        try:
            with self._tx_audio_lock:
                discard()
        except Exception as exc:
            LOGGER.debug("Discarding buffered transmit audio failed: %s", exc)

    def flush_audio(self) -> None:
        with self._operator_operation():
            self._ensure_connected_locked()
            with self._tx_audio_lock:
                self._state.tx_audio_packets += self.connectivity.flush_audio()

    def scope_configure(self, physical_side: str = "MAIN", enabled: bool = True, span_hz: int | None = None) -> dict[str, Any]:
        if type(enabled) is not bool:
            raise ValueError("Scope enabled must be a boolean")
        if span_hz is not None and (type(span_hz) is not int or span_hz not in (5000, 10000, 20000, 50000, 100000, 200000, 500000, 1000000)):
            raise ValueError("Unsupported spectrum width")
        with self._operator_operation():
            self._ensure_connected_locked()
            self._scope_configure_locked(physical_side, enabled, span_hz)
            return self._state.to_dict()

    def _scope_configure_locked(self, physical_side: str, enabled: bool, span_hz: int | None) -> None:
        side = self._normalize_side(physical_side)
        # Scope selection is independent of operating MAIN/SUB and VFO A/B.
        self._civ_transaction_locked(self._frame(0x27, bytes([0x12, 0 if side == "MAIN" else 1])))
        if span_hz is not None:
            side_code = bytes([0 if side == "MAIN" else 1])
            # The API/UI use full visible width; CI-V carries +/- half-span.
            span = side_code + self._bcd_frequency(span_hz // 2)
            self._civ_transaction_locked(self._frame(0x27, b"\x14" + side_code + b"\x00"))
            self._civ_transaction_locked(self._frame(0x27, b"\x15" + span))
            response = self._civ_transaction_locked(self._frame(0x27, b"\x15" + side_code))
            if response[6:-1] != span:
                raise IcomRadioError("Spectrum span readback did not match the requested width")
        self._civ_transaction_locked(self._frame(0x27, bytes([0x10, 0x01 if enabled else 0x00])))
        self._civ_transaction_locked(self._frame(0x27, bytes([0x11, 0x01 if enabled else 0x00])))

    def _run(self) -> None:
        reconnect_delay_s = 1.0
        attempt = 0
        while not self._stop.is_set():
            attempt += 1
            with self._lock:
                self._state.connecting = True
                self._connection_attempt = attempt
                self._retry_in_s = None
            try:
                self.connect()
                with self._lock:
                    self._state.connecting = False
                    self._connection_attempt = 0
                    self._retry_in_s = None
                reconnect_delay_s = 1.0
                attempt = 0
                reconcile_failures = 0
                while not self._stop.is_set():
                    self._service_once(0.02)
                    now = monotonic()
                    with self._lock:
                        self._service_ptt_confirmation_locked(now)
                        if self._transmit_silence_due_locked(now):
                            self._feed_transmit_silence(now)
                        transmit_active = self._transmit_is_active()
                        # Unattributed radio changes only ever arm this cheap
                        # targeted read; they never trigger the slow inventory.
                        if (self._external_refresh_due is not None
                                and now >= self._external_refresh_due
                                and not transmit_active):
                            self._refresh_external_change_locked(now)
                        elif transmit_active and now >= self._tx_meter_due:
                            self._read_tx_meters_locked(now)
                        # Low-priority telemetry: never while keyed, during a PTT
                        # release, or ahead of a queued operator command.
                        inventory_due = (
                            now - self._last_reconcile >= self._RECONCILE_INTERVAL_S
                            and not transmit_active
                            and self._state.ptt_pending is None
                        )
                    if inventory_due:
                        with self._lock:
                            reconcile_failures = self._service_reconcile_locked(now, reconcile_failures)
            except Exception as exc:
                with self._lock:
                    if self._stop.is_set():
                        break
                    LOGGER.warning(
                        "Native Icom connection lost transport=%s stage=%s error_type=%s error=%s; retrying in %.1fs",
                        self.connectivity.kind,
                        self.connectivity.snapshot().get("connection_stage", self._stage),
                        type(exc).__name__,
                        exc,
                        reconnect_delay_s,
                    )
                    self._state.last_error = str(exc)
                    self._release_ptt_best_effort("connection loss")
                    self.connectivity.disconnect()
                    self._clear_disconnected_state_locked(preserve_connecting=True)
                    self._retry_in_s = reconnect_delay_s
                if self._stop.wait(reconnect_delay_s):
                    break
                reconnect_delay_s = min(15.0, reconnect_delay_s * 2.0)
        with self._lock:
            self.connectivity.disconnect()
            self._clear_disconnected_state_locked()
            self._connection_attempt = 0
            self._retry_in_s = None
            self._stage = "idle"

    def _service_reconcile_locked(self, now: float, reconcile_failures: int) -> int:
        """Refresh radio state, deferring the full inventory for live control work.

        The inventory is roughly twenty CI-V transactions. Starting it while an
        operator command (PTT, a write, microphone audio) is queued is what made
        live control wait behind telemetry. When a caller is waiting, keep the
        cheap CI-V traffic the radio's scope output depends on and skip the rest
        until the next cycle.
        """
        self._last_reconcile = now
        self._inventory_runs += 1
        self._bump(self._civ_counts, "inventory")
        try:
            if self._operator_is_waiting():
                self._read_dualwatch_locked()
            else:
                self._prime_frequency_state_locked()
        except IcomRadioError as exc:
            reconcile_failures += 1
            self._state.last_error = str(exc)
            if reconcile_failures >= 3:
                raise IcomRadioError(
                    f"Native IC-9700 telemetry failed {reconcile_failures} times: {exc}"
                ) from exc
            return reconcile_failures
        self._state.last_error = None
        return 0

    def _debug(self, message: str, *args: object) -> None:
        if self.config.debug_logging:
            LOGGER.info("icom_debug stage=%s " + message, self._stage, *args)

    def _service_once(self, timeout_s: float = 0.0) -> None:
        with self._lock:
            incoming = self.connectivity.poll(timeout_s)
            for chunk in incoming.control_chunks:
                self._receive_civ_locked(chunk)
            for pcm in incoming.audio_chunks:
                packet = self._browser_audio_packet(pcm)
                with self._audio_lock:
                    self._audio_queue.append(packet)
                    for queue in self._audio_subscribers:
                        queue.append(packet)
                self._state.rx_audio_packets += 1

    def _select_locked(self, physical_side: str, vfo: str | None) -> None:
        self._select_count += 1
        # Attribute the switch to its caller's thread so a stray poller is
        # obvious without more traffic captures.
        thread = current_thread().name
        self._select_threads[thread] = self._select_threads.get(thread, 0) + 1
        self._civ_transaction_locked(self._frame(0x07, bytes([0xD0 if physical_side == "MAIN" else 0xD1])))
        if self._read_side_locked() != physical_side:
            raise IcomRadioError("Radio MAIN/SUB selection readback did not match; write cancelled")
        if vfo is not None:
            self._civ_transaction_locked(self._frame(0x07, bytes([0x00 if vfo == "A" else 0x01])))
            setattr(self._state, f"{physical_side.lower()}_vfo", vfo)
            self._state.vfo = vfo

    def _read_side_locked(self) -> str:
        response = self._civ_transaction_locked(self._frame(0x07, b"\xd2"))
        if len(response) != 8 or response[6] not in (0, 1):
            raise IcomRadioError("Invalid IC-9700 MAIN/SUB readback")
        side = "SUB" if response[6] else "MAIN"
        self._state.physical_side = side
        self._state.vfo = getattr(self._state, f"{side.lower()}_vfo")
        return side

    def _read_current_locked(self) -> None:
        side = self._read_side_locked()
        response = self._civ_transaction_locked(self._frame(0x03))
        value = self._decode_frequency_response(response)
        if value is None:
            raise IcomRadioError("Invalid IC-9700 frequency readback")
        mode = self._civ_transaction_locked(self._frame(0x04))
        if len(mode) not in (7, 8):
            raise IcomRadioError("Invalid IC-9700 mode/filter readback")
        filter_number = mode[6] if len(mode) == 8 and mode[6] in (1, 2, 3) else None
        bandwidth = self._read_bandwidth_locked(mode[5], filter_number)
        # Front-panel selection can change even when we are the sole LAN owner.
        if self._read_side_locked() != side:
            raise IcomRadioError("MAIN/SUB changed during readback; state not applied")
        self._set_frequency_state(side, self._state.vfo, value)
        setattr(self._state, f"{side.lower()}_mode", {
            0: "LSB", 1: "USB", 2: "AM", 3: "CW", 5: "FM", 7: "CW-R", 0x17: "DV", 0x22: "DD"
        }.get(mode[5]))
        setattr(self._state, f"{side.lower()}_filter", filter_number)
        setattr(self._state, f"{side.lower()}_bandwidth_hz", bandwidth)
        setattr(self._state, f"{side.lower()}_updated", monotonic())
        ptt = self._civ_transaction_locked(self._frame(0x1C, b"\x00"))
        if len(ptt) == 8 and ptt[6] in (0, 1):
            self._state.ptt = bool(ptt[6])

    def _read_frequency_state_locked(self, side: str) -> int:
        """Verify only frequency after an interactive frequency write."""

        response = self._civ_transaction_locked(self._frame(0x03))
        value = self._decode_frequency_response(response)
        if value is None:
            raise IcomRadioError("Invalid IC-9700 frequency readback")
        if self._read_side_locked() != side:
            raise IcomRadioError("MAIN/SUB changed during frequency readback")
        self._set_frequency_state(side, getattr(self._state, f"{side.lower()}_vfo"), value)
        return value

    def _read_mode_state_locked(self, side: str) -> tuple[str | None, int | None]:
        """Verify mode/filter without repeating unrelated frequency/PTT reads."""

        response = self._civ_transaction_locked(self._frame(0x04))
        if len(response) not in (7, 8):
            raise IcomRadioError("Invalid IC-9700 mode/filter readback")
        filter_number = response[6] if len(response) == 8 and response[6] in (1, 2, 3) else None
        bandwidth = self._read_bandwidth_locked(response[5], filter_number)
        if self._read_side_locked() != side:
            raise IcomRadioError("MAIN/SUB changed during mode readback")
        mode = {
            0: "LSB", 1: "USB", 2: "AM", 3: "CW", 5: "FM", 7: "CW-R", 0x17: "DV", 0x22: "DD"
        }.get(response[5])
        setattr(self._state, f"{side.lower()}_mode", mode)
        setattr(self._state, f"{side.lower()}_filter", filter_number)
        setattr(self._state, f"{side.lower()}_bandwidth_hz", bandwidth)
        setattr(self._state, f"{side.lower()}_updated", monotonic())
        return mode, filter_number

    def _prime_frequency_state_locked(self) -> None:
        self._read_dualwatch_locked()
        if self._operator_is_waiting():
            return
        previous_side = self._read_side_locked()
        # Read the side the radio is already parked on. Telemetry must never
        # toggle the operator's MAIN/SUB selection: that churn was visible on
        # the radio's own front panel and dominated the CI-V command load.
        self._read_current_locked()
        if self._operator_is_waiting():
            return
        self._read_levels_locked()
        if previous_side == "SUB":
            try:
                self._read_sub_rit_locked()
            except IcomRadioError as exc:
                self._state.sub_rit_hz = self._state.sub_rit_enabled = None
                self._debug("SUB RIT readback unavailable: %s", exc)
        if self._operator_is_waiting():
            return
        self._read_microphone_config_locked()
        self._refresh_inactive_side_locked()

    def _refresh_inactive_side_locked(self) -> None:
        """Occasionally refresh the side the radio is not parked on.

        The inactive side otherwise only updates when Pi-Sat writes to it (TX
        Doppler does exactly that), so a rare read keeps the console honest
        without paying for a MAIN/SUB switch on every telemetry cycle.
        """
        now = monotonic()
        if now < self._inactive_side_due or self._operator_is_waiting():
            return
        self._inactive_side_due = now + self._INACTIVE_SIDE_INTERVAL_S
        previous_side = self._state.physical_side
        other = "SUB" if previous_side == "MAIN" else "MAIN"
        if other == "SUB" and self._state.dualwatch is False:
            return  # Never switch on a disabled receiver just to poll it.
        try:
            self._select_locked(other, None)
            self._read_current_locked()
            # Levels live per side, so the console's inactive-side AF/RF/
            # SQUELCH controls stay disabled unless this read happens too.
            self._read_levels_locked()
            if other == "SUB":
                try:
                    self._read_sub_rit_locked()
                except IcomRadioError:
                    pass
        except IcomRadioError as exc:
            self._debug("Inactive side refresh skipped: %s", exc)
        finally:
            try:
                self._select_locked(previous_side, None)
            except IcomRadioError as exc:
                self._debug("Inactive side restore skipped: %s", exc)

    def _prime_operating_state_locked(self) -> None:
        """Read only state needed to make the console operational at startup."""

        self._read_dualwatch_locked()
        previous_side = self._read_side_locked()
        selected_side = previous_side
        try:
            for side in ("MAIN", "SUB"):
                if side == "SUB" and self._state.dualwatch is False:
                    continue
                if side != selected_side:
                    self._select_locked(side, None)
                    selected_side = side
                self._read_current_locked()
        finally:
            if selected_side != previous_side:
                self._select_locked(previous_side, None)

    def _read_bandwidth_locked(self, mode: int, filter_number: int | None) -> int | None:
        # IC-9700 Basic manual 4-4: FM/DV presets are fixed; SSB/CW/AM
        # are user-adjustable, so FIL number alone cannot determine their width.
        if mode in (0x05, 0x17):
            return {1: 15000, 2: 10000, 3: 7000}.get(filter_number)
        if mode == 0x22:
            return 150000
        if mode not in (0, 1, 2, 3, 7):
            return None
        response = self._civ_transaction_locked(self._frame(0x1A, b"\x03"))
        if len(response) != 8 or response[6] >> 4 > 9 or response[6] & 15 > 9:
            raise IcomRadioError("Invalid IF bandwidth readback")
        code = (response[6] >> 4) * 10 + (response[6] & 15)
        if mode == 2 and code <= 49:
            return (code + 1) * 200
        if mode != 2 and code <= 40:
            return (code + 1) * 50 if code < 10 else (code - 4) * 100
        raise IcomRadioError("IF bandwidth readback outside the mode's range")

    @staticmethod
    def _decode_level(response: bytes) -> int:
        if len(response) != 9 or any(byte >> 4 > 9 or byte & 15 > 9 for byte in response[6:8]):
            raise IcomRadioError("Invalid radio level readback")
        value = int(response[6:8].hex())
        if value > 255:
            raise IcomRadioError("Radio level readback outside 0..255")
        return value

    def _read_levels_locked(self) -> None:
        side = self._read_side_locked()
        values = {}
        for control, command, subcommand in (("af_gain", 0x14, 1), ("rf_gain", 0x14, 2),
                                              ("squelch", 0x14, 3), ("s_meter", 0x15, 2)):
            values[control] = self._decode_level(self._civ_transaction_locked(self._frame(command, bytes([subcommand]))))
        sql = self._civ_transaction_locked(self._frame(0x15, b"\x01"))
        if len(sql) != 8 or sql[6] not in (0, 1):
            raise IcomRadioError("Invalid squelch status readback")
        values["squelch_open"] = bool(sql[6])
        if self._read_side_locked() != side:
            raise IcomRadioError("MAIN/SUB changed during level readback; state not applied")
        for key, value in values.items():
            setattr(self._state, f"{side.lower()}_{key}", value)
        # TX meters are read by _read_tx_meters_locked on their own keyed-only
        # cadence so the slow inventory can stay out of the way during transmit.

    def _receive_civ_locked(self, chunk: bytes) -> None:
        """Consume an opaque control byte chunk supplied by any transport."""
        self._serial_buffer.extend(chunk)
        while b"\xfd" in self._serial_buffer:
            end = self._serial_buffer.index(0xFD) + 1
            frame = bytes(self._serial_buffer[:end])
            del self._serial_buffer[:end]
            begin = frame.find(b"\xfe\xfe")
            if begin < 0:
                continue
            frame = frame[begin:]
            if (len(frame) < 6 or frame[3] != self.config.civ_address
                    or frame[2] not in (0, self.config.controller_address)):
                continue  # Ignore controller echoes and other CI-V addresses.
            if frame[4:6] == b"\x27\x00":
                scope_line = self._normalize_scope_frame_locked(frame)
                if scope_line is not None:
                    with self._scope_lock:
                        self._latest_scope = scope_line
                        self._scope_queue.append(scope_line)
                        self._state.scope_lines += 1
                continue
            request = self._pending_civ
            if request is not None and frame[2] == self.config.controller_address:
                query = request[4] in (0x03, 0x04) or request[4:-1] in (
                    b"\x07\xd2", b"\x1c\x00", b"\x1a\x03", b"\x14\x01", b"\x14\x02",
                    b"\x14\x03", b"\x15\x01", b"\x15\x02", b"\x15\x11", b"\x15\x12", b"\x15\x14",
                    b"\x21\x00", b"\x21\x01")
                query = query or request[4:-1] in (b"\x16\x59", b"\x1a\x05\x01\x14", b"\x1a\x05\x01\x15", b"\x1a\x05\x01\x16")
                query = query or (request[4:6] == b"\x27\x15" and len(request) == 8)
                matched = (frame[4] == request[4] and
                           (request[4] in (0x03, 0x04) or frame[5:6] == request[5:6])) if query else frame[4] == 0xFB
                if query and request[4:6] == b"\x27\x15":
                    matched = matched and frame[6:7] == request[6:7]
                if query and request[4:6] == b"\x1a\x05":
                    matched = matched and frame[6:8] == request[6:8]
                if frame[4] == 0xFA or matched:
                    self._civ_response = frame
                    continue
            if frame[4] in (0x00, 0x01):
                self._classify_transceive_locked(frame)
        if len(self._serial_buffer) > 4096:
            self._serial_buffer.clear()

    def _scope_count_locked(self, side: int, key: str, *, maximum: int | None = None,
                            current: int | None = None) -> None:
        entry = self._scope_counts.setdefault(
            "main" if side == 0 else "sub",
            {"frames": 0, "lines": 0, "single_frame_lines": 0, "discarded": 0,
             "last_maximum": 0, "last_current": 0},
        )
        entry[key] = entry.get(key, 0) + 1
        if maximum is not None:
            entry["last_maximum"] = maximum
        if current is not None:
            entry["last_current"] = current

    def _normalize_scope_frame_locked(self, frame: bytes) -> bytes | None:
        """Normalize one-carrier and divided scope output to one 475-bin frame.

        IC-9700 CI-V command 27 00 may arrive as a single complete sweep or as
        a sequence of division frames whose last two are numbered 0x10 and
        0x11. The first division carries waveform metadata; the rest carry the
        475 bins between them. A sweep emitted as divisions only produces a
        drawable line once every division has arrived, so it refreshes far more
        slowly than a single-frame sweep even though the radio is sending
        steadily. Keep that difference out of the connectivity modules and
        present one identical CI-V scope line to the rest of Pi-Sat.
        """

        if len(frame) < 10:
            return None
        side, current, maximum = frame[6], frame[7], frame[8]
        if side not in (0, 1):
            return None
        self._scope_count_locked(side, "frames", maximum=maximum, current=current)
        if maximum == 1:
            self._scope_divisions.pop(side, None)
            if current != 1:
                self._scope_count_locked(side, "discarded")
                return None
            self._scope_count_locked(side, "lines")
            self._scope_count_locked(side, "single_frame_lines")
            return frame
        usb_order = (*range(0x01, 0x0A), 0x10, 0x11)
        if maximum != 0x11 or current not in usb_order:
            self._scope_divisions.pop(side, None)
            self._scope_count_locked(side, "discarded")
            return None
        if current == 0x01:
            # Header + side/current/maximum + mode/frequencies/range + FD.
            if len(frame) != 22:
                self._scope_divisions.pop(side, None)
                self._scope_count_locked(side, "discarded")
                return None
            self._scope_divisions[side] = (frame[:21], 0x02, bytearray())
            return None
        assembly = self._scope_divisions.get(side)
        if assembly is None or assembly[1] != current:
            self._scope_divisions.pop(side, None)
            self._scope_count_locked(side, "discarded")
            return None
        metadata, _, bins = assembly
        bins.extend(frame[9:-1])
        if current != maximum:
            self._scope_divisions[side] = (metadata, usb_order[usb_order.index(current) + 1], bins)
            return None
        self._scope_divisions.pop(side, None)
        if len(bins) != 475:
            self._scope_count_locked(side, "discarded")
            return None
        self._scope_count_locked(side, "lines")
        normalized = bytearray(metadata)
        normalized[7] = normalized[8] = 1
        normalized.extend(bins)
        normalized.append(0xFD)
        return bytes(normalized)

    def _civ_transaction_locked(self, frame: bytes) -> bytes:
        self._stage = "civ_transaction"
        self._ensure_connected_locked()
        self._service_once()  # Consume old ACKs before issuing a new request.
        self._pending_civ = frame
        self._civ_response = None
        self.connectivity.write_control(frame)
        deadline = monotonic() + 1.5
        try:
            while monotonic() < deadline and not self._stop.is_set():
                # Continue servicing audio, pings and token renewal while CAT
                # waits; scope frames are routed separately from command replies.
                self._service_once(0.005)
                if self._civ_response is not None:
                    if self._civ_response[4] == 0xFA:
                        raise IcomRadioError("IC-9700 returned CI-V NAK")
                    return self._civ_response
                # poll() already waits for transport input. A short yield keeps
                # this loop from spinning when the transport returns early.
                sleep(0.001)
            self._debug("CI-V timeout bytes=%d command=%02x", len(frame), frame[4] if len(frame) > 4 else 0)
            raise IcomRadioError("Timed out waiting for IC-9700 CI-V response")
        finally:
            self._pending_civ = None
            self._civ_response = None
            if self._state.connected:
                self._stage = "connected"

    # --- Transceive routing, targeted refresh and transport diagnostics -------
    # The radio pushes untagged CI-V 0x00 (frequency) and 0x01 (mode) frames.
    # They cannot say which side or VFO they belong to, so each frame is either
    # matched against a write we just issued (self-echo, free to apply) or
    # treated as an unattributed change that only arms a cheap targeted read.
    # The slow telemetry inventory is never triggered from this path.
    _EXTERNAL_REFRESH_DEBOUNCE_S = 0.2
    _ECHO_DUPLICATE_WINDOW_S = 0.75
    _TX_METER_INTERVAL_S = 1.0
    _MODE_NAMES = {0: "LSB", 1: "USB", 2: "AM", 3: "CW", 5: "FM", 7: "CW-R",
                   0x17: "DV", 0x22: "DD"}

    @staticmethod
    def _bump(counter: dict[str, int], key: str) -> None:
        counter[key] = counter.get(key, 0) + 1

    @staticmethod
    def _echo_vfo(physical_side: str, vfo: str | None) -> str:
        """Encode side+VFO in the one history field that accepts them."""
        return f"{'Main' if physical_side == 'MAIN' else 'Sub'}{vfo or ''}"

    @staticmethod
    def _side_from_echo_vfo(echo_vfo: str | None) -> str | None:
        prefix = (echo_vfo or "")[:4].lower()
        if prefix.startswith("main"):
            return "MAIN"
        if prefix.startswith("sub"):
            return "SUB"
        return None

    @staticmethod
    def _vfo_from_echo_vfo(echo_vfo: str | None) -> str | None:
        letter = (echo_vfo or "")[-1:].upper()
        return letter if letter in {"A", "B"} else None

    def _classify_transceive_locked(self, frame: bytes) -> None:
        now = monotonic()
        is_frequency = frame[4] == 0x00
        name = (RadioStateProperty.FREQUENCY.value if is_frequency
                else RadioStateProperty.MODE.value)
        value = (self._decode_frequency_response(frame) if is_frequency
                 else (frame[5] if len(frame) > 5 else None))
        if value is None:
            self._bump(self._transceive_counts, "ambiguous")
            self._arm_external_refresh_locked(name, now)
            return
        matched = self._recent_commands.match(
            RadioStateEvent(
                property=(RadioStateProperty.FREQUENCY if is_frequency
                          else RadioStateProperty.MODE),
                value=value,
                timestamp=now,
                source="iciv_transceive",
            ),
            now,
        )
        if matched is not None:
            self._apply_self_echo_locked(is_frequency, value, matched.vfo)
            self._confirmed_echoes.append((name, value, now))
            self._bump(self._transceive_counts, "self_echo")
            return
        if self._matches_confirmed_echo_locked(name, value, now):
            # The LAN transport can deliver the same frame twice. A duplicate of
            # an echo we already applied must not look like an external change.
            self._bump(self._transceive_counts, "duplicate")
            return
        self._bump(self._transceive_counts, "external")
        self._arm_external_refresh_locked(name, now)

    def _matches_confirmed_echo_locked(self, name: str, value: object, now: float) -> bool:
        while (self._confirmed_echoes
               and now - self._confirmed_echoes[0][2] > self._ECHO_DUPLICATE_WINDOW_S):
            self._confirmed_echoes.popleft()
        return any(entry_name == name and entry_value == value
                   for entry_name, entry_value, _at in self._confirmed_echoes)

    def _apply_self_echo_locked(self, is_frequency: bool, value: int, echo_vfo: str | None) -> None:
        side = self._side_from_echo_vfo(echo_vfo)
        if side is None:
            return
        vfo = self._vfo_from_echo_vfo(echo_vfo)
        if is_frequency:
            self._set_frequency_state(side, vfo, int(value))
            return
        mode = self._MODE_NAMES.get(value)
        if mode is not None:
            setattr(self._state, f"{side.lower()}_mode", mode)

    def _arm_external_refresh_locked(self, name: str, now: float) -> None:
        self._external_refresh_properties.add(name)
        self._external_refresh_due = now + self._EXTERNAL_REFRESH_DEBOUNCE_S

    def _refresh_external_change_locked(self, now: float) -> None:
        """Cheap, non-switching reconciliation for a change we did not command.

        Reads only the currently selected side's frequency and/or mode. Levels,
        RIT, microphone settings and meters stay on the slow inventory, and a
        failure here is reported without ever feeding the reconcile counter.
        """
        pending = self._external_refresh_properties
        self._external_refresh_properties = set()
        self._external_refresh_due = None
        if not pending or not self._state.connected:
            return
        self._bump(self._civ_counts, "targeted_refresh")
        try:
            side = self._read_side_locked()
            if RadioStateProperty.FREQUENCY.value in pending:
                self._read_frequency_state_locked(side)
            if RadioStateProperty.MODE.value in pending:
                self._read_mode_state_locked(side)
            self._targeted_refresh_runs += 1
        except IcomError as exc:
            self._debug("External-change refresh skipped: %s", exc)

    def _read_tx_meters_locked(self, now: float) -> None:
        """Best-effort TX meters. Never counted as a reconcile failure, and it
        only selects MAIN when the operator is not already there."""
        self._tx_meter_due = now + self._TX_METER_INTERVAL_S
        if not self._state.connected or self._state.ptt is not True:
            return
        self._bump(self._civ_counts, "tx_meters")
        try:
            previous_side = self._read_side_locked()
            if previous_side != "MAIN":
                self._select_locked("MAIN", None)
            try:
                meters = {
                    name: self._decode_level(
                        self._civ_transaction_locked(self._frame(0x15, bytes([subcommand])))
                    )
                    for name, subcommand in (("power", 0x11), ("swr", 0x12), ("compression", 0x14))
                }
                if self._read_side_locked() == "MAIN":
                    for name, value in meters.items():
                        setattr(self._state, f"{name}_meter", value)
                    self._state.tx_meter_updated = monotonic()
            finally:
                if previous_side != "MAIN":
                    self._select_locked(previous_side, None)
        except IcomError as exc:
            self._debug("TX meter read skipped: %s", exc)

    def _transport_diagnostics_locked(self) -> dict[str, Any]:
        return {
            "civ_counts": dict(self._civ_counts),
            "transceive_counts": dict(self._transceive_counts),
            "select_count": self._select_count,
            "select_threads": dict(self._select_threads),
            "inventory_runs": self._inventory_runs,
            "targeted_refresh_runs": self._targeted_refresh_runs,
            "lock_wait_max_ms": round(self._lock_wait_max_s * 1000, 1),
            "lock_hold_max_ms": round(self._lock_hold_max_s * 1000, 1),
            "tx_audio_write_max_ms": round(self._tx_audio_write_max_s * 1000, 1),
            "scope_counts": {key: dict(value) for key, value in self._scope_counts.items()},
            "tx_audio_epoch": self._tx_audio_epoch,
            "tx_audio_ptt_commanded": self._ptt_commanded,
            "tx_audio_paced": self._transmit_paced,
        }

    def _bump_tx_audio_epoch_locked(self) -> None:
        """Invalidate in-flight microphone frames. Called with the controller lock."""
        self._tx_audio_epoch += 1

    def _browser_audio_packet(self, pcm: bytes) -> bytes:
        """Wrap transport-neutral PCM in Pi-Sat's existing browser envelope."""
        packet = bytearray(24 + len(pcm))
        struct.pack_into("<I", packet, 0, len(packet))
        struct.pack_into("<H", packet, 4, 0)
        struct.pack_into(">H", packet, 18, self._browser_audio_seq)
        struct.pack_into(">H", packet, 22, len(pcm))
        packet[24:] = pcm
        self._browser_audio_seq = (self._browser_audio_seq + 1) & 0xFFFF
        return bytes(packet)

    def _frame(self, command: int, data: bytes = b"") -> bytes:
        return b"\xfe\xfe" + bytes([self.config.civ_address, self.config.controller_address, command]) + data + b"\xfd"

    @staticmethod
    def _bcd_frequency(frequency_hz: int) -> bytes:
        if not 0 < frequency_hz < 10_000_000_000:
            raise ValueError("Frequency is outside the five-byte CI-V range")
        digits = f"{frequency_hz:010d}"
        return bytes(
            (int(digits[index]) << 4) | int(digits[index + 1])
            for index in range(8, -1, -2)
        )

    @staticmethod
    def _normalize_side(value: str) -> str:
        normalized = str(value).strip().upper()
        if normalized not in {"MAIN", "SUB"}:
            raise ValueError("physical_side must be MAIN or SUB")
        return normalized

    @staticmethod
    def _normalize_vfo(value: str) -> str:
        normalized = str(value).strip().upper().replace("VFO", "")
        if normalized not in {"A", "B"}:
            raise ValueError("vfo must be A or B")
        return normalized

    @staticmethod
    def _resolve_microphone_source(source: str | int) -> int:
        """Map a microphone source name or raw code to its IC-9700 DATA MOD value."""
        if type(source) is int:
            if source in MICROPHONE_SOURCES.values():
                return source
            raise ValueError("Microphone source code must be 0 to 5")
        key = str(source).strip().lower()
        if key in MICROPHONE_SOURCES:
            return MICROPHONE_SOURCES[key]
        raise ValueError("Microphone source must be MIC, ACC, MIC+ACC, USB, MIC+USB or LAN")

    def _set_frequency_state(self, side: str, vfo: str | None, frequency_hz: int) -> None:
        setattr(self._state, f"{side.lower()}_frequency_hz", frequency_hz)
        if vfo is not None:
            setattr(self._state, f"{side.lower()}_{vfo.lower()}_hz", frequency_hz)

    @staticmethod
    def _decode_frequency_response(response: bytes) -> int | None:
        digits = response[5:-1]
        if (len(response) != 11 or response[4] not in (0x00, 0x03)
                or any((byte & 15) > 9 or (byte >> 4) > 9 for byte in digits)):
            return None
        value = 0
        multiplier = 1
        for byte in digits[:5]:
            value += (byte & 0x0F) * multiplier
            multiplier *= 10
            value += ((byte >> 4) & 0x0F) * multiplier
            multiplier *= 10
        return value

    def _ensure_connected_locked(self) -> None:
        if not self._state.connected or self._stop.is_set():
            raise IcomRadioError("Radio is disconnected; use the Radio page Connect button")

    def _clear_disconnected_state_locked(self, *, preserve_connecting: bool = False) -> None:
        self._state.connected = False
        self._state.connecting = preserve_connecting
        self._state.authenticated = False
        self._serial_buffer.clear()
        with self._audio_lock:
            self._audio_queue.clear()
            for queue in self._audio_subscribers:
                queue.clear()
        with self._scope_lock:
            self._scope_queue.clear()
            self._latest_scope = None
        self._scope_divisions.clear()
        self._browser_audio_seq = 0
        self._state.ptt = None
        self._state.ptt_pending = None
        self._ptt_commanded = False
        self._transmit_paced = False
        self._ptt_idle_since = None
        self._ptt_release_deadline = None
        self._state.tx_meter_updated = None
        self._bump_tx_audio_epoch_locked()
