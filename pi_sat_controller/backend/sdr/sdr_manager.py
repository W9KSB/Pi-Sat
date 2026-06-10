from __future__ import annotations

from dataclasses import dataclass

from pi_sat_controller.backend.sdr.rigctl_sdr_client import RigctlSdrClient


@dataclass
class SdrManager:
    client: RigctlSdrClient

    def get_frequency(self) -> int:
        return self.client.get_frequency()

    def set_frequency(self, frequency_hz: int) -> None:
        self.client.set_frequency(frequency_hz)

