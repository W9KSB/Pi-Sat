"""Backend-owned standard-message QSO sequencing for the managed engine."""

from __future__ import annotations

import logging
import copy
import math
import re
import threading
import time
import uuid
from collections import deque
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any

from .contacts import WsjtxContactStore, utc_time

LOGGER = logging.getLogger(__name__)
_CALL = re.compile(r"(?:[A-Z][A-Z0-9]?|[0-9][A-Z])[0-9][A-Z]{1,3}(?:/[PR])?")
_GRID = re.compile(r"[A-R]{2}[0-9]{2}")
_REPORT = re.compile(r"([+-][0-9]{2})")
_MAX_RETRIES = 3
_TX_SLOTS = ("auto", "odd", "even")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def parse_standard_message(message: str) -> dict[str, str]:
    """Identify explicit sender and recipient without resolving hashed calls."""
    if not isinstance(message, str) or len(message) > 80 or any(char in message for char in "<>;"):
        raise ValueError("Decode is not an unambiguous standard message")
    words = message.strip().upper().split()
    if words and words[0] in ("CQ", "QRZ", "DE"):
        if len(words) == 4 and words[0] == "CQ" and re.fullmatch(r"[A-Z]{1,4}|[0-9]{3}", words[1]):
            words = [words[0], *words[2:]]
        if len(words) != 3 or not _CALL.fullmatch(words[1]) or not _GRID.fullmatch(words[2]):
            raise ValueError("Decode does not contain an explicit standard call and grid")
        return {"recipient": words[0], "sender": words[1], "payload": words[2], "grid": words[2], "report": ""}
    if len(words) != 3 or not _CALL.fullmatch(words[0]) or not _CALL.fullmatch(words[1]) or words[0] == words[1]:
        raise ValueError("Decode does not contain two distinct standard calls")
    payload = words[2]
    report = payload[1:] if payload.startswith("R") and _REPORT.fullmatch(payload[1:]) else payload if _REPORT.fullmatch(payload) else ""
    if report and not -50 <= int(report) <= 49:
        raise ValueError("Decode report is outside the standard range")
    grid = payload if payload not in ("RRR", "RR73", "73") and _GRID.fullmatch(payload) else ""
    if not grid and not report and payload not in ("RRR", "RR73", "73"):
        raise ValueError("Decode is not a supported standard exchange")
    return {"recipient": words[0], "sender": words[1], "payload": payload, "grid": grid, "report": report}


class WsjtxQso:
    def __init__(
        self, *, get_settings: Callable[[], dict[str, Any]],
        get_manager: Callable[[], Any], get_engine: Callable[[], Any],
        get_controller: Callable[[], Any], contacts: WsjtxContactStore,
        get_satellite: Callable[[], str] = lambda: "",
    ) -> None:
        self._get_settings, self._get_manager = get_settings, get_manager
        self._get_engine, self._get_controller = get_engine, get_controller
        self._get_satellite, self.contacts = get_satellite, contacts
        self._lock = threading.RLock()
        self._operation_lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._my_call = self._my_grid = self._target_call = self._target_grid = ""
        self._mode = "FT8"
        self._report = "-10"
        self._tx_offset = 1500
        self._rx_offset = 0
        self._selected = 6
        self._enabled = False
        self._auto = True
        self._transmitting = self._tx_available = False
        self._detail = "Select a live standard decode or call CQ"
        self._tx_slot = "auto"
        self._tx_even = True
        self._attempts = 0
        self._engine = self._controller = None
        self._radio_cache_controller = None
        self._radio_cache: dict[str, Any] | None = None
        self._radio_cache_at = 0.0
        self._engine_generation = self._controller_generation = None
        self._client_key = ""
        self._attempt_id = ""
        self._intent_epoch = 0
        self._seen: set[str] = set()
        self._seen_order: deque[str] = deque()
        self._was_transmitting = False
        self._inflight: dict[str, Any] | None = None
        self._time_on = self._report_sent = self._report_received = ""
        self._acknowledged = False
        self._logged = False
        self._suspended = False
        self._can_resume = False
        self._fault_sequence: int | None = None
        self._final_fault_sequence: int | None = None
        self._transmissions: deque[dict[str, Any]] = deque(maxlen=100)
        try:
            self._read_station()
        except Exception as error:
            self._detail = str(error)
        if contacts.error:
            self._detail = contacts.error

    def _read_station(self) -> None:
        settings = self._get_settings()
        call = str(settings.get("callsign", "")).upper().strip()
        grid = str(settings.get("grid_locator", "")).upper().strip()
        mode = str(settings.get("mode", "FT8")).upper()
        if not _CALL.fullmatch(call) or not re.fullmatch(r"[A-R]{2}[0-9]{2}(?:[A-X]{2}(?:[0-9]{2})?)?", grid):
            raise ValueError("Configure a standard station callsign and valid grid before transmitting")
        if mode not in ("FT8", "FT4"):
            raise ValueError("QSO sequencing supports FT8 and FT4")
        self._my_call, self._my_grid, self._mode = call, grid[:4], mode

    def _messages(self) -> list[dict[str, Any]]:
        pair = f"{self._target_call} {self._my_call}" if self._target_call else ""
        return [{"number": number, "text": text} for number, text in (
            (1, f"{pair} {self._my_grid}" if pair else ""),
            (2, f"{pair} {self._report}" if pair else ""),
            (3, f"{pair} R{self._report}" if pair else ""),
            (4, f"{pair} RR73" if pair else ""), (5, f"{pair} 73" if pair else ""),
            (6, f"CQ {self._my_call} {self._my_grid}" if self._my_call else ""),
        )]

    def _period_ms(self) -> int:
        return 7500 if self._mode == "FT4" else 15000

    def _reply_tx_even(self, decode_time_ms: int) -> bool:
        """Return WSJT-X's even/first-period choice for a partner decode.

        WSJT-X's ``Tx even/1st`` control selects the period after an odd
        partner period.  The decode timestamp is relative to UTC midnight,
        which keeps this calculation aligned with the protocol rather than
        with local wall-clock or process start time.  FT4 timestamps are
        truncated to whole seconds by WSJT-X, so ``:22`` represents the
        ``:22.5`` slot and must be rounded to the nearest slot boundary before
        its parity is selected.  A timestamp exactly on a boundary belongs to
        that starting slot.
        """
        if type(decode_time_ms) is not int or not 0 <= decode_time_ms < 86400000:
            raise ValueError("Decode has no valid native time")
        period_ms = self._period_ms()
        decode_slot = (decode_time_ms + period_ms // 2) // period_ms
        return decode_slot % 2 == 1

    def _latest_partner_tx_even(self, manager: dict[str, Any]) -> bool | None:
        """Return Auto parity from the newest live decode of the selected partner."""
        with self._lock:
            target, client_key = self._target_call, self._client_key
        if not target or not client_key:
            return None
        for decode in manager.get("decodes", []):
            try:
                parsed = self._live_decode(decode, client_key)
            except ValueError:
                continue
            if parsed["sender"] == target:
                return self._reply_tx_even(decode["time_ms"])
        return None

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            next_slot = ""
            if self._enabled:
                period = self._period_ms() / 1000
                slot = math.floor(_now().timestamp() / period)
                if (slot % 2 == 0) != self._tx_even:
                    slot += 1
                next_slot = datetime.fromtimestamp(slot * period, timezone.utc).isoformat(timespec="milliseconds")
            return {
                "my_call": self._my_call, "my_grid": self._my_grid,
                "target_call": self._target_call, "target_grid": self._target_grid,
                "report": self._report, "tx_offset_hz": self._tx_offset,
                "rx_offset_hz": self._rx_offset, "selected_message": self._selected,
                "messages": self._messages(), "enabled": self._enabled,
                "auto_sequence": self._auto, "transmitting": self._transmitting,
                "tx_available": self._tx_available, "retries": max(0, self._attempts - 1),
                "max_retries": _MAX_RETRIES, "detail": self._detail,
                "suspended": self._suspended, "can_resume": self._can_resume,
                "tx_slot": self._tx_slot, "tx_even": self._tx_even,
                "next_slot_utc": next_slot, "contacts": self.contacts.list(),
                "transmissions": copy.deepcopy(list(self._transmissions)),
            }

    def _radio(self, controller: Any) -> dict[str, Any]:
        if controller is None:
            raise RuntimeError("Native radio is unavailable")
        reader = getattr(controller, "try_snapshot", None)
        radio = reader() if reader is not None else controller.snapshot()
        if radio is None and controller is self._radio_cache_controller and self._radio_cache is not None and time.monotonic() - self._radio_cache_at <= 2:
            return self._radio_cache
        if radio is None:
            self._radio_cache = None
            raise RuntimeError("A fresh native radio snapshot is unavailable")
        if not isinstance(radio, dict) or not radio.get("connected"):
            self._radio_cache = None
            raise RuntimeError("Native radio is disconnected")
        if radio.get("main", {}).get("mode") not in ("USB", "USB-DATA", "LSB", "LSB-DATA") or radio.get("sub", {}).get("mode") not in ("USB", "USB-DATA"):
            self._radio_cache = None
            raise RuntimeError("Native MAIN must use USB/LSB and SUB must use USB, with optional DATA")
        self._radio_cache_controller, self._radio_cache = controller, radio
        self._radio_cache_at = time.monotonic()
        return radio

    @staticmethod
    def _radio_generation(controller: Any) -> int | None:
        reader = getattr(controller, "connection_generation", None)
        value = reader() if callable(reader) else reader
        return value if type(value) is int else None

    def _context(self) -> tuple[Any, dict[str, Any], dict[str, Any], Any, dict[str, Any]]:
        if self.contacts.error:
            raise RuntimeError(self.contacts.error)
        engine = self._get_engine()
        status = engine.snapshot()
        tx = status.get("tx", {})
        with self._lock:
            self._tx_available = bool(tx.get("available"))
            self._transmitting = bool(tx.get("transmitting"))
        if (not status.get("running") or not tx.get("available") or not tx.get("connected") or
                tx.get("mode") != self._mode or type(tx.get("generation")) is not int or
                type(tx.get("transmitting")) is not bool or type(tx.get("enabled")) is not bool or
                not isinstance(tx.get("message"), str)):
            raise RuntimeError("Managed WSJT-X transmit controls and fresh receive source are unavailable")
        manager = self._get_manager().snapshot()
        if not manager.get("running") or not manager.get("enabled"):
            raise RuntimeError("WSJT-X listener is unavailable")
        controller = self._get_controller()
        return engine, tx, manager, controller, self._radio(controller)

    def _owned_client(self, manager: dict[str, Any], tx: dict[str, Any]) -> str:
        clients = [client for client in manager.get("clients", []) if client.get("connected") and client.get("id") == "WSJT-X - PiSat-RX" and client.get("host") in ("127.0.0.1", "::1") and client.get("status", {}).get("mode") == self._mode]
        if tx.get("client_key"):
            clients = [client for client in clients if client.get("key") == tx["client_key"]]
        if len(clients) != 1:
            raise RuntimeError("A unique fresh managed WSJT-X client is required")
        seen = utc_time(clients[0]["last_seen_utc"])
        if not -2 <= (_now() - seen).total_seconds() <= 45:
            raise RuntimeError("Managed WSJT-X client status is stale")
        return clients[0]["key"]

    def _live_decode(self, decode: dict[str, Any], client_key: str) -> dict[str, str]:
        if decode.get("client_key") != client_key or decode.get("mode") != self._mode:
            raise ValueError("Decode is not from the active managed mode and client")
        if decode.get("low_confidence") or decode.get("off_air"):
            raise ValueError("Off-air or low-confidence decodes cannot select or advance a QSO")
        if type(decode.get("time_ms")) is not int or not 0 <= decode["time_ms"] < 86400000 or type(decode.get("frequency_hz")) is not int or decode["frequency_hz"] < 0:
            raise ValueError("Decode has no valid native time and frequency")
        for field in ("received_at_utc", "time_utc"):
            try:
                age = (_now() - utc_time(decode[field])).total_seconds()
            except (KeyError, TypeError, ValueError) as error:
                raise ValueError("Decode has no valid live timestamp") from error
            if not -2 <= age <= self._period_ms() / 500:
                raise ValueError("Decode is stale")
        return parse_standard_message(decode.get("message", ""))

    def _offset_valid(self, hz: int, radio: dict[str, Any]) -> None:
        bandwidth = radio.get("sub", {}).get("bandwidth_hz")
        bandwidth = bandwidth if type(bandwidth) is int and bandwidth > 0 else 3000
        width = 90 if self._mode == "FT4" else 50
        if type(hz) is not int or not 200 <= hz <= bandwidth - width:
            raise ValueError(f"TX offset must keep all {self._mode} tones inside the {bandwidth} Hz audio span")

    def _remember(self, key: str) -> None:
        if key not in self._seen:
            self._seen.add(key)
            self._seen_order.append(key)
        while len(self._seen_order) > 5000:
            self._seen.discard(self._seen_order.popleft())

    def _new_attempt(
        self, engine: Any, tx: dict[str, Any], manager: dict[str, Any], controller: Any,
        epoch: int,
    ) -> None:
        client_key = self._owned_client(manager, tx)
        with self._lock:
            if self._intent_epoch != epoch:
                raise RuntimeError("Transmit intent was superseded")
            self._engine, self._controller = engine, controller
            self._engine_generation = tx.get("generation")
            self._controller_generation = self._radio_generation(controller)
            self._client_key = client_key
            self._attempt_id = uuid.uuid4().hex
            self._time_on = self._report_sent = self._report_received = ""
            self._acknowledged = self._logged = False
            self._attempts = 0
            self._inflight = None
            self._was_transmitting = bool(tx.get("transmitting"))
            self._suspended = self._can_resume = False
            self._fault_sequence = None
            self._final_fault_sequence = None
            self._seen.clear()
            self._seen_order.clear()
            for decode in manager.get("decodes", []):
                self._remember(decode["key"])

    def _intent_is_current(self, epoch: int) -> bool:
        with self._lock:
            return self._intent_epoch == epoch

    def _intent_epoch_value(self) -> int:
        with self._lock:
            return self._intent_epoch

    @staticmethod
    def _fault_has_contract_fields(fault: Any) -> bool:
        return (
            isinstance(fault, dict)
            and type(fault.get("sequence")) is int
            and isinstance(fault.get("category"), str)
            and isinstance(fault.get("detail"), str)
        )

    @classmethod
    def _fault_is_recoverable(cls, fault: Any) -> bool:
        return cls._fault_has_contract_fields(fault) and fault.get("recoverable") is True

    @staticmethod
    def _fault_is_pending(fault: Any, tx: dict[str, Any]) -> bool:
        return WsjtxQso._fault_has_contract_fields(fault) and tx.get("fault_pending") is True

    @staticmethod
    def _fault_description(fault: Any) -> str:
        if not isinstance(fault, dict):
            return "Managed transmit state is unavailable"
        detail = str(fault.get("detail") or "").strip()
        category = str(fault.get("category") or "").strip()
        return detail or category or "Managed transmit fault"

    def _abort_inflight_locked(self) -> None:
        interrupted = self._inflight
        self._inflight = None
        if interrupted is None:
            return
        for transmission in self._transmissions:
            if transmission.get("key") == interrupted.get("history_key"):
                transmission.update(
                    completed=False,
                    aborted=True,
                    time_off_utc=_now().isoformat(timespec="milliseconds"),
                )
                break

    def _suspend_for_fault(
        self, engine: Any, fault: dict[str, Any], *, transmitting: bool,
    ) -> None:
        sequence = fault["sequence"]
        with self._lock:
            if self._suspended and self._final_fault_sequence == sequence:
                return
            self._intent_epoch += 1
            self._enabled = False
            self._transmitting = False
            self._tx_available = False
            self._suspended = True
            self._can_resume = True
            self._fault_sequence = sequence
            self._final_fault_sequence = sequence
            self._was_transmitting = False
            self._detail = (
                f"TX suspended after {self._fault_description(fault)}; "
                "the same contact can be resumed explicitly"
            )
            self._abort_inflight_locked()
        try:
            engine.halt_tx()
        except Exception as error:
            with self._lock:
                self._detail += f"; transmit halt failed: {error}"
        LOGGER.warning("QSO transmit suspended: %s", self._fault_description(fault))

    def _suspend_pending_fault(
        self, engine: Any, *, fault_sequence: int | None = None,
        abort_inflight: bool = False,
    ) -> None:
        with self._lock:
            if self._suspended and self._fault_sequence == fault_sequence and not self._can_resume:
                if abort_inflight:
                    self._abort_inflight_locked()
                return
            self._intent_epoch += 1
            self._enabled = False
            self._transmitting = False
            self._tx_available = False
            self._suspended = True
            self._can_resume = False
            self._fault_sequence = fault_sequence
            self._was_transmitting = False
            self._detail = (
                "TX suspended while the managed engine assesses a transmit failure; "
                "the contact is retained"
            )
            if abort_inflight:
                self._abort_inflight_locked()
        try:
            engine.halt_tx()
        except Exception as error:
            with self._lock:
                self._detail += f"; transmit halt failed: {error}"

    def _resume_context_valid(
        self,
        engine: Any,
        status: dict[str, Any],
        tx: dict[str, Any],
        manager: dict[str, Any],
        controller: Any,
        *,
        require_available: bool,
    ) -> bool:
        with self._lock:
            bound_engine = self._engine
            bound_controller = self._controller
            engine_generation = self._engine_generation
            controller_generation = self._controller_generation
            mode = self._mode
            client_key = self._client_key
            tx_offset = self._tx_offset
        if self.contacts.error or engine is not bound_engine or controller is not bound_controller:
            return False
        try:
            settings_mode = str(self._get_settings().get("mode", "FT8")).upper()
        except (TypeError, ValueError, RuntimeError):
            return False
        if (not status.get("running") or not tx.get("connected") or
                tx.get("generation") != engine_generation or tx.get("mode") != mode or
                settings_mode != mode):
            return False
        if require_available and (not tx.get("available") or tx.get("fault") is not None):
            return False
        fault = tx.get("fault")
        if fault is not None and not self._fault_is_recoverable(fault):
            return False
        if not manager.get("running") or not manager.get("enabled"):
            return False
        try:
            if self._owned_client(manager, tx) != client_key:
                return False
            if self._radio_generation(controller) != controller_generation:
                return False
            self._offset_valid(tx_offset, self._radio(controller))
        except (RuntimeError, ValueError):
            return False
        return True

    def _set_resume_state(self, can_resume: bool) -> None:
        with self._lock:
            self._can_resume = bool(self._suspended and can_resume)

    def _configure(self, epoch: int) -> None:
        if not self._intent_is_current(epoch):
            raise RuntimeError("Transmit intent was superseded")
        with self._lock:
            message = self._messages()[self._selected - 1]["text"]
            offset_hz, tx_even = self._tx_offset, self._tx_even
        if not message:
            raise ValueError("Select a target before choosing a directed message")
        engine = self._engine or self._get_engine()
        engine.configure_tx(message=message, offset_hz=offset_hz, tx_even=tx_even)
        if not self._intent_is_current(epoch):
            raise RuntimeError("Transmit intent was superseded")
        LOGGER.info("QSO configured for %s at %d Hz", self._target_call or "CQ", offset_hz)

    def _halt(self, detail: str) -> None:
        with self._lock:
            self._intent_epoch += 1
            self._enabled = False
            self._detail = detail
            self._abort_inflight_locked()
            self._suspended = self._can_resume = False
            self._fault_sequence = None
            self._final_fault_sequence = None
            engine = self._engine
        try:
            (engine or self._get_engine()).halt_tx()
        except Exception as error:
            with self._lock:
                self._detail += f"; transmit halt failed: {error}"
        LOGGER.info("QSO transmit disabled: %s", self._detail)

    def _arm(self, engine: Any, radio: dict[str, Any], epoch: int) -> None:
        self._offset_valid(self._tx_offset, radio)
        try:
            self._configure(epoch)
            engine.set_tx_enabled(
                True, cancelled=lambda: not self._intent_is_current(epoch),
            )
            with self._lock:
                if self._intent_epoch != epoch:
                    raise RuntimeError("Transmit intent was superseded")
                self._enabled = True
                self._detail = "Transmit armed for the selected slot"
        except Exception as error:
            if self._intent_is_current(epoch):
                self._halt(f"Could not configure or enable managed transmission: {error}")
            raise

    def select_target(self, decode_key: str) -> dict[str, Any]:
        if not isinstance(decode_key, str) or not decode_key or len(decode_key) > 128:
            raise ValueError("A decode key is required")
        with self._operation_lock:
            start_epoch = self._intent_epoch_value()
            self._read_station()
            engine, tx, manager, controller, radio = self._context()
            client = self._owned_client(manager, tx)
            decode = next((item for item in manager.get("decodes", []) if item.get("key") == decode_key), None)
            if decode is None:
                raise ValueError("Selected decode is no longer available")
            parsed = self._live_decode(decode, client)
            if parsed["sender"] == self._my_call:
                raise ValueError("Cannot select the station's own decode")
            snr = decode.get("snr_db")
            if type(snr) is not int or not -50 <= snr <= 49:
                raise ValueError("Decode has no standard signal report")
            with self._lock:
                if self._intent_epoch != start_epoch:
                    raise RuntimeError("Transmit intent was superseded")
                self._target_call, self._target_grid = parsed["sender"], parsed["grid"]
                self._report = f"{snr:+03d}"
                self._rx_offset = decode.get("frequency_hz", 0)
                if self._tx_slot == "auto":
                    self._tx_even = self._reply_tx_even(decode["time_ms"])
                self._selected = 1
                self._intent_epoch += 1
                epoch = self._intent_epoch
            self._new_attempt(engine, tx, manager, controller, epoch)
            if parsed["recipient"] == self._my_call:
                self._apply_reply(parsed)
            self._arm(engine, radio, epoch)
            LOGGER.info("QSO selected sender %s from live %s decode", self._target_call, self._mode)
            return self.snapshot()

    def call_cq(self) -> dict[str, Any]:
        with self._operation_lock:
            start_epoch = self._intent_epoch_value()
            self._read_station()
            engine, tx, manager, controller, radio = self._context()
            with self._lock:
                if self._intent_epoch != start_epoch:
                    raise RuntimeError("Transmit intent was superseded")
                self._target_call = self._target_grid = ""
                self._selected = 6
                if self._tx_slot == "auto":
                    self._tx_even = True
                self._intent_epoch += 1
                epoch = self._intent_epoch
            self._new_attempt(engine, tx, manager, controller, epoch)
            self._arm(engine, radio, epoch)
            return self.snapshot()

    def select_message(self, number: int) -> dict[str, Any]:
        if type(number) is not int or not 1 <= number <= 6:
            raise ValueError("Message number must be from 1 to 6")
        with self._operation_lock:
            start_epoch = self._intent_epoch_value()
            with self._lock:
                if not self._messages()[number - 1]["text"]:
                    raise ValueError("Select a target before choosing a directed message")
                self._selected, self._attempts = number, 0
                if self._intent_epoch != start_epoch:
                    raise RuntimeError("Transmit intent was superseded")
                self._intent_epoch += 1
                epoch = self._intent_epoch
                enabled = self._enabled
            if enabled:
                try:
                    self._configure(epoch)
                except Exception as error:
                    if self._intent_is_current(epoch):
                        self._halt(f"Could not change the managed transmit message: {error}")
                    raise
            with self._lock:
                self._detail = f"Message {number} selected; auto sequence {'enabled' if self._auto else 'disabled'}"
            return self.snapshot()

    def set_offset(self, hz: int) -> dict[str, Any]:
        with self._operation_lock:
            start_epoch = self._intent_epoch_value()
            radio = self._radio(self._get_controller())
            self._offset_valid(hz, radio)
            with self._lock:
                if self._intent_epoch != start_epoch:
                    raise RuntimeError("Transmit intent was superseded")
                self._tx_offset = hz
                self._intent_epoch += 1
                epoch = self._intent_epoch
                enabled = self._enabled
                transmitting = self._transmitting
            if enabled:
                try:
                    self._configure(epoch)
                except Exception as error:
                    if self._intent_is_current(epoch):
                        self._halt(f"Could not change the managed transmit offset: {error}")
                    raise
            with self._lock:
                self._detail = "TX offset applies to the next burst" if transmitting else "TX offset updated"
            return self.snapshot()

    def set_tx_slot(self, slot: str) -> dict[str, Any]:
        if type(slot) is not str or slot not in _TX_SLOTS:
            raise ValueError("TX slot must be auto, odd, or even")
        with self._operation_lock:
            start_epoch = self._intent_epoch_value()
            inferred_even = None
            if slot == "auto":
                inferred_even = self._latest_partner_tx_even(
                    self._get_manager().snapshot(),
                )
            with self._lock:
                if self._intent_epoch != start_epoch:
                    raise RuntimeError("Transmit intent was superseded")
                self._tx_slot = slot
                if inferred_even is not None:
                    self._tx_even = inferred_even
                elif slot != "auto":
                    self._tx_even = slot == "even"
                self._intent_epoch += 1
                epoch = self._intent_epoch
                enabled = self._enabled
                transmitting = self._transmitting
            if enabled:
                try:
                    self._configure(epoch)
                except Exception as error:
                    if self._intent_is_current(epoch):
                        self._halt(f"Could not change the managed transmit slot: {error}")
                    raise
            with self._lock:
                if transmitting:
                    self._detail = "TX slot applies to the next burst"
                elif slot == "auto" and inferred_even is None:
                    self._detail = "Auto TX slot selected; no live partner decode is available"
                else:
                    self._detail = f"TX slot set to {slot}"
            return self.snapshot()

    def _resume_suspended(self, start_epoch: int) -> dict[str, Any]:
        engine = self._get_engine()
        status = engine.snapshot()
        tx = status.get("tx", {})
        manager = self._get_manager().snapshot()
        controller = self._get_controller()
        if not self._resume_context_valid(
            engine, status, tx, manager, controller, require_available=False,
        ):
            detail = "Faulted contact cannot resume because the engine, client, mode, radio, or attempt session changed"
            self._halt(detail)
            raise RuntimeError(detail)
        recover = getattr(engine, "recover_tx", None)
        if not callable(recover):
            detail = "Managed WSJT-X TX recovery is unavailable"
            self._halt(detail)
            raise RuntimeError(detail)
        try:
            recover()
        except Exception as error:
            if not self._intent_is_current(start_epoch):
                raise RuntimeError("Transmit intent was superseded") from error
            detail = f"Managed WSJT-X TX recovery failed: {error}"
            refreshed = engine.snapshot()
            self._set_resume_state(self._resume_context_valid(
                engine, refreshed, refreshed.get("tx", {}),
                self._get_manager().snapshot(), controller,
                require_available=False,
            ))
            with self._lock:
                self._detail = detail
            raise RuntimeError(detail) from error
        status = engine.snapshot()
        tx = status.get("tx", {})
        manager = self._get_manager().snapshot()
        if not self._intent_is_current(start_epoch):
            raise RuntimeError("Transmit intent was superseded")
        if not self._resume_context_valid(
            engine, status, tx, manager, controller, require_available=True,
        ):
            if self._resume_context_valid(
                engine, status, tx, manager, controller, require_available=False,
            ):
                detail = str((tx.get("fault") or {}).get("detail")
                             or status.get("detail") or "TX recovery is not ready")
                self._set_resume_state(not tx.get("fault_pending"))
                with self._lock:
                    self._detail = detail
                raise RuntimeError(detail)
            detail = "Managed WSJT-X TX recovery completed, but the contact session is no longer resumable"
            self._halt(detail)
            raise RuntimeError(detail)
        radio = self._radio(controller)
        with self._lock:
            if self._intent_epoch != start_epoch:
                raise RuntimeError("Transmit intent was superseded")
            self._intent_epoch += 1
            epoch = self._intent_epoch
            self._suspended = False
            self._can_resume = False
            self._fault_sequence = None
            self._final_fault_sequence = None
            self._tx_available = True
            self._was_transmitting = False
        self._arm(engine, radio, epoch)
        with self._lock:
            self._detail = (
                f"Transmit resumed for {self._target_call or 'CQ'} at message {self._selected}; "
                "the interrupted burst remains incomplete"
            )
        return self.snapshot()

    def set_enabled(self, enabled: bool) -> dict[str, Any]:
        if type(enabled) is not bool:
            raise ValueError("enabled must be a boolean")
        if not enabled:
            self._halt("Transmit disabled by the operator")
            return self.snapshot()
        with self._operation_lock:
            start_epoch = self._intent_epoch_value()
            with self._lock:
                already_enabled = self._enabled
                suspended = self._suspended
            if suspended:
                return self._resume_suspended(start_epoch)
            if not already_enabled:
                self._read_station()
                engine, tx, manager, controller, radio = self._context()
                with self._lock:
                    if self._intent_epoch != start_epoch:
                        raise RuntimeError("Transmit intent was superseded")
                    self._intent_epoch += 1
                    epoch = self._intent_epoch
                self._new_attempt(engine, tx, manager, controller, epoch)
                self._arm(engine, radio, epoch)
            return self.snapshot()

    def set_auto_sequence(self, enabled: bool) -> dict[str, Any]:
        if type(enabled) is not bool:
            raise ValueError("enabled must be a boolean")
        with self._lock:
            self._auto = enabled
            return self.snapshot()

    def _apply_reply(self, parsed: dict[str, str], *, advance: bool = True,
                     decode_time_ms: int | None = None) -> bool:
        with self._lock:
            if parsed["recipient"] != self._my_call or parsed["sender"] != self._target_call:
                return False
            if parsed["grid"]:
                self._target_grid = parsed["grid"]
                stage = 2
            elif parsed["report"]:
                self._report_received = parsed["report"]
                if parsed["payload"].startswith("R"):
                    self._acknowledged = True
                    stage = 4
                else:
                    stage = 3
            elif parsed["payload"] in ("RR73", "RRR", "73"):
                self._acknowledged = True
                stage = 5
            else:
                return False
            if (self._tx_slot == "auto" and type(decode_time_ms) is int
                    and 0 <= decode_time_ms < 86400000):
                # Every valid partner decode determines the immediately
                # following compatible slot. This matters when decoder output
                # arrives near the end of the partner's transmission window.
                self._tx_even = self._reply_tx_even(decode_time_ms)
            if advance and (self._selected == 6 or stage > self._selected):
                self._selected, self._attempts = stage, 0
                return True
            return False

    def _log_contact(self, radio: dict[str, Any]) -> None:
        if self._logged or not self._time_on or not self._report_sent or not self._report_received or not self._acknowledged:
            return
        rx, tx = radio.get("sub", {}).get("frequency_hz"), radio.get("main", {}).get("frequency_hz")
        if type(rx) is not int or type(tx) is not int or rx <= 0 or tx <= 0:
            raise RuntimeError("Confirmed native frequencies are required to log the completed contact")
        satellite = self._get_satellite()
        map_receive_frequency = getattr(self._engine, "rx_audio_frequency_hz", None)
        corrected_rx = map_receive_frequency(self._rx_offset) if callable(map_receive_frequency) else None
        receive_frequency = corrected_rx if corrected_rx is not None else (
            rx - self._rx_offset if str(radio.get("sub", {}).get("mode", "")).startswith("LSB") else rx + self._rx_offset
        )
        self.contacts.append({
            "id": self._attempt_id, "time_on_utc": self._time_on, "time_off_utc": _now().isoformat(),
            "call": self._target_call, "grid": self._target_grid, "mode": self._mode,
            "report_sent": self._report_sent, "report_received": self._report_received,
            "satellite": satellite if isinstance(satellite, str) else "",
            "rx_frequency_hz": receive_frequency,
            "tx_frequency_hz": tx - self._tx_offset if str(radio.get("main", {}).get("mode", "")).startswith("LSB") else tx + self._tx_offset,
        })
        self._logged = True
        LOGGER.info("QSO contact logged: %s %s", self._target_call, self._mode)
        self._halt(f"Contact with {self._target_call} completed and logged")

    def _process_decodes(
        self, manager: dict[str, Any], client_key: str, *, passive: bool,
    ) -> None:
        for decode in reversed(manager.get("decodes", [])):
            key = decode.get("key", "")
            with self._lock:
                unseen = bool(key) and key not in self._seen
                if unseen:
                    self._remember(key)
            if not unseen:
                continue
            try:
                parsed = self._live_decode(decode, client_key)
            except ValueError:
                continue
            if passive:
                with self._lock:
                    advanced = self._apply_reply(
                        parsed, advance=self._auto,
                        decode_time_ms=decode.get("time_ms"),
                    )
                    if advanced:
                        self._detail = (
                            f"Addressed reply from {self._target_call} retained while TX is suspended; "
                            f"message {self._selected} is ready for explicit resume"
                        )
                continue
            with self._lock:
                adopt_sender = (
                    self._auto and not self._target_call and self._selected == 6
                    and parsed["recipient"] == self._my_call
                    and parsed["sender"] != self._my_call
                )
            if adopt_sender:
                snr = decode.get("snr_db")
                if type(snr) is int and -50 <= snr <= 49:
                    with self._lock:
                        self._target_call, self._target_grid = parsed["sender"], parsed["grid"]
                        self._report = f"{snr:+03d}"
                        self._rx_offset = decode.get("frequency_hz", 0)
            with self._lock:
                advanced = self._apply_reply(
                    parsed, advance=self._auto,
                    decode_time_ms=decode.get("time_ms"),
                )
                epoch = self._intent_epoch
            if advanced:
                self._configure(epoch)
                with self._lock:
                    self._detail = (
                        f"Addressed reply from {self._target_call}; "
                        f"advancing to message {self._selected}"
                    )
                    target, selected = self._target_call, self._selected
                LOGGER.info("QSO addressed RX advanced %s to stage %d", target, selected)

    def _poll_once(self) -> None:
        with self._operation_lock:
            engine = self._get_engine()
            status = engine.snapshot()
            tx = status.get("tx", {})
            fault = tx.get("fault")
            transmitting = bool(tx.get("transmitting"))
            with self._lock:
                self._transmitting = transmitting
                self._tx_available = bool(tx.get("available"))
                enabled = self._enabled
                suspended = self._suspended
                was_transmitting = self._was_transmitting
                bound_engine = self._engine
                bound_controller = self._controller
                engine_generation = self._engine_generation
                controller_generation = self._controller_generation
                mode = self._mode
                client_key = self._client_key
                tx_offset = self._tx_offset
            if fault is not None:
                if self._fault_is_pending(fault, tx):
                    if enabled or suspended:
                        self._suspend_pending_fault(
                            engine, fault_sequence=fault["sequence"],
                            abort_inflight=True,
                        )
                    return
                if self._fault_is_recoverable(fault):
                    if enabled or suspended:
                        self._suspend_for_fault(engine, fault, transmitting=transmitting)
                        manager = self._get_manager().snapshot()
                        controller = self._get_controller()
                        can_resume = self._resume_context_valid(
                            engine, status, tx, manager, controller,
                            require_available=False,
                        )
                        self._set_resume_state(can_resume)
                        if can_resume:
                            self._process_decodes(manager, client_key, passive=True)
                    return
                if enabled or suspended:
                    self._halt(
                        "Managed WSJT-X transmit fault is not recoverable: "
                        f"{self._fault_description(fault)}"
                    )
                return
            if (enabled or suspended) and not tx.get("available"):
                manager = self._get_manager().snapshot()
                controller = self._get_controller()
                if self._resume_context_valid(
                    engine, status, tx, manager, controller,
                    require_available=False,
                ):
                    self._suspend_pending_fault(engine)
                else:
                    self._halt(
                        "Engine, native source, mode, or control session changed "
                        "while TX failure assessment was pending"
                    )
                return
            if suspended:
                manager = self._get_manager().snapshot()
                controller = self._get_controller()
                can_resume = self._resume_context_valid(
                    engine, status, tx, manager, controller,
                    require_available=False,
                )
                self._set_resume_state(can_resume)
                if can_resume:
                    self._process_decodes(manager, client_key, passive=True)
                return
            if not enabled:
                with self._lock:
                    self._was_transmitting = transmitting
                return
            settings = self._get_settings()
            controller = self._get_controller()
            if (engine is not bound_engine or controller is not bound_controller or
                    not status.get("running") or not tx.get("available") or not tx.get("connected") or
                    type(tx.get("enabled")) is not bool or type(tx.get("transmitting")) is not bool or not isinstance(tx.get("message"), str) or
                    tx.get("generation") != engine_generation or tx.get("mode") != mode or
                    str(settings.get("mode", "FT8")).upper() != mode or
                    self._radio_generation(controller) != controller_generation):
                self._halt("Engine, native source, mode, or control session changed; transmit is disarmed")
                return
            radio = self._radio(controller)
            manager = self._get_manager().snapshot()
            if not manager.get("running") or not manager.get("enabled") or self._owned_client(manager, tx) != client_key:
                self._halt("Managed receive client changed; transmit is disarmed")
                return
            self._offset_valid(tx_offset, radio)
            mismatch = False
            with self._lock:
                if transmitting and not was_transmitting:
                    actual = str(tx.get("message", "")).strip().upper()
                    transmission = {
                        "key": uuid.uuid4().hex, "time_utc": _now().isoformat(timespec="milliseconds"),
                        "mode": tx.get("mode"), "message": actual, "frequency_hz": tx.get("offset_hz"),
                        "client_key": client_key, "generation": tx.get("generation"),
                        "direction": "tx", "transmitted": True, "completed": False,
                    }
                    self._transmissions.appendleft(transmission)
                    expected = self._messages()[self._selected - 1]["text"]
                    if actual != expected:
                        mismatch = True
                    else:
                        self._attempts += 1
                        self._time_on = self._time_on or _now().isoformat()
                        self._inflight = {
                            "number": self._selected, "attempt": self._attempt_id,
                            "message": actual, "history_key": transmission["key"],
                        }
                        LOGGER.info("QSO actual TX started: stage %d attempt %d", self._selected, self._attempts)
                        if self._selected in (2, 3):
                            self._report_sent = actual.split()[-1].removeprefix("R")
            if mismatch:
                self._halt("Observed transmission does not match the selected standard message")
                return
            finished = None
            with self._lock:
                if not transmitting and was_transmitting and self._inflight is not None:
                    finished = self._inflight
                    self._inflight = None
                    for transmission in self._transmissions:
                        if transmission["key"] == finished["history_key"]:
                            transmission.update(completed=True, time_off_utc=_now().isoformat(timespec="milliseconds"))
                            break
            if finished is not None:
                LOGGER.info("QSO actual TX completed: stage %d", finished["number"])
                with self._lock:
                    log_contact = (
                        finished["attempt"] == self._attempt_id
                        and finished["number"] in (4, 5)
                    )
                if log_contact:
                    self._log_contact(radio)
                    with self._lock:
                        still_enabled = self._enabled
                    if not still_enabled:
                        with self._lock:
                            self._was_transmitting = False
                        return
                with self._lock:
                    retries_exhausted = (
                        finished["number"] == self._selected
                        and self._attempts >= _MAX_RETRIES + 1
                    )
                if retries_exhausted:
                    self._halt("Initial transmission and three unanswered repeats completed")
                    with self._lock:
                        self._was_transmitting = False
                    return
            with self._lock:
                self._was_transmitting = transmitting
            self._process_decodes(manager, client_key, passive=False)

    def start(self) -> None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop.clear()
            self._thread = threading.Thread(target=self._run, name="wsjtx-qso", daemon=True)
            self._thread.start()

    def _run(self) -> None:
        while not self._stop.wait(0.1):
            try:
                self._poll_once()
            except Exception as error:
                with self._lock:
                    previous = self._detail
                    enabled = self._enabled
                if enabled:
                    self._halt(f"QSO sequencing stopped: {error}")
                else:
                    with self._lock:
                        self._tx_available = False
                        self._detail = f"QSO controls unavailable: {error}"
                with self._lock:
                    changed = self._detail != previous
                if changed:
                    LOGGER.warning("QSO poll failed: %s", error)

    def shutdown(self) -> None:
        self._stop.set()
        self._halt("QSO sequencing stopped")
        with self._lock:
            worker = self._thread
        if worker is not None:
            worker.join(timeout=1.0)
