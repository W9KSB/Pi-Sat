from __future__ import annotations

from collections import deque
from dataclasses import dataclass, replace
from enum import StrEnum
import logging
from threading import Lock
from time import monotonic
from typing import Any


LOGGER = logging.getLogger(__name__)

RECENT_COMMAND_HISTORY_SIZE = 16
RECENT_COMMAND_WINDOW_S = 2.0
ASYNC_ECHO_FREQUENCY_TOLERANCE_HZ = 0
ASYNC_RECONCILIATION_POLL_S = 5.0
ASYNC_RECONCILIATION_MISS_THRESHOLD = 3
PENDING_EXTERNAL_EVENT_WINDOW_S = 5.0
ASYNC_ROLE_ROUTE_MAX_DISTANCE_HZ = 2_000_000
ASYNC_ROLE_ROUTE_MIN_ADVANTAGE_HZ = 100_000


class RadioStateProperty(StrEnum):
    FREQUENCY = "frequency"
    MODE = "mode"
    VFO = "vfo"
    PTT = "ptt"


class RadioStateClassification(StrEnum):
    SELF_ECHO = "self_echo"
    EXTERNAL_CHANGE = "external_change"
    STATE_REFRESH = "state_refresh"


class AsyncCapabilityState(StrEnum):
    UNSUPPORTED = "unsupported"
    AVAILABLE = "available"
    VERIFIED = "verified"


@dataclass(frozen=True)
class RadioStateEvent:
    property: RadioStateProperty
    value: Any
    timestamp: float
    role: str | None = None
    vfo: str | None = None
    source: str = "hamlib_async"
    classification: RadioStateClassification = RadioStateClassification.STATE_REFRESH
    requires_reconciliation: bool = False

    def with_route(
        self,
        role: str | None,
        classification: RadioStateClassification | None = None,
        requires_reconciliation: bool | None = None,
    ) -> "RadioStateEvent":
        return replace(
            self,
            role=role,
            classification=classification or self.classification,
            requires_reconciliation=(
                self.requires_reconciliation
                if requires_reconciliation is None
                else requires_reconciliation
            ),
        )


@dataclass(frozen=True)
class RecentRadioCommand:
    property: RadioStateProperty
    value: Any
    sent_at: float
    role: str | None = None
    vfo: str | None = None
    source: str = "pi_sat"


@dataclass(frozen=True)
class RadioFrequencyObservation:
    frequency_hz: int | None
    classification: RadioStateClassification
    timestamp: float
    from_poll: bool = False
    error: str | None = None


class RecentCommandHistory:
    """Thread-safe, time-bounded history used to identify async command echoes."""

    def __init__(
        self,
        max_entries: int = RECENT_COMMAND_HISTORY_SIZE,
        window_s: float = RECENT_COMMAND_WINDOW_S,
    ) -> None:
        self.max_entries = max(1, int(max_entries))
        self.window_s = max(0.1, float(window_s))
        self._commands: deque[RecentRadioCommand] = deque(maxlen=self.max_entries)
        self._lock = Lock()

    def record(
        self,
        property: RadioStateProperty,
        value: Any,
        role: str | None = None,
        vfo: str | None = None,
        source: str = "pi_sat",
        sent_at: float | None = None,
    ) -> RecentRadioCommand:
        command = RecentRadioCommand(
            property=property,
            value=value,
            role=_normalize_role(role),
            vfo=normalize_event_vfo(vfo),
            sent_at=monotonic() if sent_at is None else float(sent_at),
            source=source,
        )
        with self._lock:
            self._expire_locked(command.sent_at)
            self._commands.append(command)
        return command

    def match(
        self,
        event: RadioStateEvent,
        now: float | None = None,
    ) -> RecentRadioCommand | None:
        checked_at = monotonic() if now is None else float(now)
        event_role = _normalize_role(event.role)
        event_vfo = normalize_event_vfo(event.vfo)
        with self._lock:
            self._expire_locked(checked_at)
            candidates: list[tuple[int, RecentRadioCommand]] = []
            for index, command in enumerate(self._commands):
                if command.property != event.property:
                    continue
                if not _values_match(command, event.value):
                    continue
                if event_role and command.role and event_role != command.role:
                    continue
                if event_vfo and command.vfo and event_vfo != command.vfo:
                    continue
                candidates.append((index, command))

            if not candidates:
                return None

            if event_role is None or event_vfo is None:
                route_keys = {(item.role, item.vfo) for _, item in candidates}
                if len(route_keys) != 1:
                    LOGGER.debug(
                        "async recent-command match ambiguous property=%s role=%s vfo=%s value=%s candidates=%s",
                        event.property,
                        event.role,
                        event.vfo,
                        event.value,
                        len(candidates),
                    )
                    return None

            index, command = candidates[-1]
            del self._commands[index]
            LOGGER.debug(
                "async recent-command matched property=%s role=%s vfo=%s value=%s age_ms=%s",
                command.property,
                command.role,
                command.vfo,
                command.value,
                round((checked_at - command.sent_at) * 1000),
            )
            return command

    def expire(self, now: float | None = None) -> list[RecentRadioCommand]:
        checked_at = monotonic() if now is None else float(now)
        with self._lock:
            return self._expire_locked(checked_at)

    def clear(self) -> None:
        with self._lock:
            self._commands.clear()

    def _expire_locked(self, now: float) -> list[RecentRadioCommand]:
        expired: list[RecentRadioCommand] = []
        while self._commands and now - self._commands[0].sent_at > self.window_s:
            command = self._commands.popleft()
            expired.append(command)
            LOGGER.debug(
                "async recent-command expired property=%s role=%s vfo=%s value=%s age_ms=%s",
                command.property,
                command.role,
                command.vfo,
                command.value,
                round((now - command.sent_at) * 1000),
            )
        return expired


def normalize_event_vfo(vfo: str | None) -> str | None:
    value = (vfo or "").strip().upper().replace("_", "")
    if not value or value in {"CURRENT", "CURR", "VFOCURR", "NONE"}:
        return None
    mapping = {
        "A": "VFOA",
        "B": "VFOB",
        "VFOA": "VFOA",
        "VFOB": "VFOB",
        "MAIN": "Main",
        "SUB": "Sub",
        "MAINA": "MainA",
        "MAINB": "MainB",
        "MAINC": "MainC",
        "SUBA": "SubA",
        "SUBB": "SubB",
        "SUBC": "SubC",
    }
    return mapping.get(value, vfo.strip())


def _normalize_role(role: str | None) -> str | None:
    value = (role or "").strip().lower()
    return value if value in {"rx", "tx"} else None


def _values_match(command: RecentRadioCommand, event_value: Any) -> bool:
    if command.property == RadioStateProperty.FREQUENCY:
        try:
            return (
                abs(int(command.value) - int(event_value))
                <= ASYNC_ECHO_FREQUENCY_TOLERANCE_HZ
            )
        except (TypeError, ValueError):
            return False
    if command.property == RadioStateProperty.MODE:
        return str(command.value).strip().upper() == str(event_value).strip().upper()
    return command.value == event_value
