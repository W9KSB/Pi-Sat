from __future__ import annotations

from dataclasses import dataclass, field
from time import monotonic

from pi_sat_controller.backend.models import MasterMode


@dataclass
class CommandOwnership:
    last_write_source: str | None = None
    last_write_time: float = 0.0
    self_update_window_s: float = 0.5

    def mark_write(self, source: str) -> None:
        self.last_write_source = source
        self.last_write_time = monotonic()

    def is_recent_self_update(self, source: str) -> bool:
        return (
            self.last_write_source == source
            and monotonic() - self.last_write_time <= self.self_update_window_s
        )


@dataclass
class SyncState:
    master_mode: MasterMode = MasterMode.MANUAL
    ownership: CommandOwnership = field(default_factory=CommandOwnership)
