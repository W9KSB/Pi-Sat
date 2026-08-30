from __future__ import annotations

from types import SimpleNamespace
import unittest

from pi_sat_controller.backend.controller.rx_tracking import RxTrackingManager
from pi_sat_controller.backend.models import SatelliteProfile, TransponderProfile
from pi_sat_controller.backend.orbital.orbital_engine import SatellitePosition
from pi_sat_controller.backend.radio.radio_state import (
    RadioFrequencyObservation,
    RadioStateClassification,
)
from pi_sat_controller.backend.radio.shared_radio_controller import SharedLocalRadioController
from pi_sat_controller.backend.radio.radio_manager import RadioOperationDeferred


class FakeTrackingSdr:
    def __init__(self, frequency_hz: int) -> None:
        self.frequency_hz = frequency_hz
        self.observations: list[RadioFrequencyObservation] = []
        self.writes: list[int] = []

    def snapshot(self):
        return SimpleNamespace(frequency_hz=self.frequency_hz, error=None)

    def read_frequency_for_reconciliation(self):
        observation = self.observations.pop(0)
        if observation.frequency_hz is not None:
            self.frequency_hz = observation.frequency_hz
        return observation

    def try_set_frequency(self, frequency_hz: int):
        self.frequency_hz = frequency_hz
        self.writes.append(frequency_hz)
        return SimpleNamespace(error=None)


def make_tracking_manager(sdr: FakeTrackingSdr, target_hz: int) -> RxTrackingManager:
    transponder = TransponderProfile(
        name="Test",
        type="rx_only",
        uplink_low=0,
        uplink_high=0,
        downlink_low=target_hz - 100_000,
        downlink_high=target_hz + 100_000,
        uplink_mode="FM",
        downlink_mode="FM",
        inverted=False,
        ratio=1.0,
        preferred_uplink=0,
        preferred_downlink=target_hz - 100,
    )
    satellite = SatelliteProfile("Test Sat", 12345, False, [transponder])
    manager = RxTrackingManager(
        orbital_engine=SimpleNamespace(),
        sdr_manager=sdr,
        satellite=satellite,
        transponder=transponder,
        deadband_hz=10,
        cat_rate_limit_hz=1000,
    )
    manager._active = True
    manager._last_commanded_rx_hz = target_hz
    manager._last_rx_write_at = 0.0
    manager._virtual_rit_hz = 100
    manager._rx_session_ready = True
    return manager


POSITION = SatellitePosition(0.0, 10.0, 0.0, 0.0, 1000.0, 0.0)


def tracking_profile(name: str, *, rx_only: bool) -> TransponderProfile:
    return TransponderProfile(
        name=name,
        type="rx_only" if rx_only else "linear",
        uplink_low=0 if rx_only else 145_900_000,
        uplink_high=0 if rx_only else 146_000_000,
        downlink_low=435_500_000,
        downlink_high=435_700_000,
        uplink_mode="USB",
        downlink_mode="USB",
        inverted=False,
        ratio=1.0,
        preferred_uplink=0 if rx_only else 145_950_000,
        preferred_downlink=435_600_000,
    )


class AsyncTrackingTests(unittest.TestCase):
    def test_self_echo_never_changes_manual_or_virtual_rit_offset(self) -> None:
        target = 435_612_000
        sdr = FakeTrackingSdr(target)
        sdr.observations.append(
            RadioFrequencyObservation(
                frequency_hz=target - 100,
                classification=RadioStateClassification.SELF_ECHO,
                timestamp=1.0,
            )
        )
        manager = make_tracking_manager(sdr, target)

        manager._apply_update(POSITION, write_devices=True)

        self.assertEqual(manager._user_downlink_offset_hz, 0)
        self.assertEqual(manager._virtual_rit_hz, 100)
        self.assertEqual(manager._last_observed_rx_hz, target - 100)
        self.assertEqual(sdr.writes, [])

    def test_external_async_change_uses_existing_manual_offset_reconciliation(self) -> None:
        target = 435_612_000
        external = target + 800
        sdr = FakeTrackingSdr(target)
        sdr.observations.append(
            RadioFrequencyObservation(
                frequency_hz=external,
                classification=RadioStateClassification.EXTERNAL_CHANGE,
                timestamp=1.0,
            )
        )
        manager = make_tracking_manager(sdr, target)

        manager._apply_update(POSITION, write_devices=True)

        self.assertEqual(manager._user_downlink_offset_hz, 800)
        self.assertEqual(manager._virtual_rit_hz, 100)
        self.assertEqual(sdr.writes, [])


class SyncOffsetDefaultTests(unittest.TestCase):
    def test_new_rx_tx_target_resets_sync_offsets_to_on(self) -> None:
        full_duplex = tracking_profile("Full duplex", rx_only=False)
        rx_only = tracking_profile("Receive only", rx_only=True)
        sdr = FakeTrackingSdr(full_duplex.preferred_downlink)
        manager = RxTrackingManager(
            orbital_engine=SimpleNamespace(get_position=lambda _norad: POSITION),
            sdr_manager=sdr,
            satellite=SatelliteProfile("Initial", 1, False, [full_duplex]),
            transponder=full_duplex,
            deadband_hz=10,
        )

        self.assertTrue(manager.snapshot().sync_offsets)
        manager.set_offset_sync(False)
        self.assertFalse(manager.snapshot().sync_offsets)

        manager.update_target(
            SatelliteProfile("RX only", 2, False, [rx_only]),
            rx_only,
        )
        self.assertFalse(manager.snapshot().sync_offsets)

        manager.update_target(
            SatelliteProfile("Next full duplex", 3, False, [full_duplex]),
            full_duplex,
        )
        self.assertTrue(manager.snapshot().sync_offsets)


class FakeSharedClient:
    def __init__(self) -> None:
        self.generation = 1
        self.transmitting = False
        self.ptt_reads = 0
        self.frequency_writes: list[tuple[str | None, int]] = []
        self.mode_writes: list[tuple[str | None, str, int]] = []

    def register_role_vfo(self, _role, _vfo) -> None:
        pass

    def ensure_connected(self) -> int:
        return self.generation

    def get_ptt_on_vfo(self, _vfo) -> bool:
        self.ptt_reads += 1
        return self.transmitting

    def set_frequency_on_vfo(self, vfo, frequency_hz) -> None:
        self.frequency_writes.append((vfo, frequency_hz))

    def set_mode_on_vfo(self, vfo, mode, passband_hz) -> None:
        self.mode_writes.append((vfo, mode, passband_hz))

    def set_split_on_vfo(self, *_args) -> None:
        pass


class SharedTransmitProtectionTests(unittest.TestCase):
    def test_rx_frequency_continues_while_tx_frequency_is_deferred(self) -> None:
        client = FakeSharedClient()
        controller = SharedLocalRadioController(client, "MainA", "SubA", False)
        controller.initialize()
        initial_ptt_reads = client.ptt_reads
        client.transmitting = True

        with controller.operation_batch():
            controller.set_frequency("rx", 435_612_100)

        self.assertEqual(client.frequency_writes, [("MainA", 435_612_100)])
        self.assertEqual(client.ptt_reads, initial_ptt_reads)

        latest_target = 145_912_000
        for latest_target in (145_912_100, 145_912_200, 145_912_300):
            with self.assertRaises(RadioOperationDeferred):
                with controller.operation_batch():
                    controller.set_frequency("tx", latest_target)

        client.transmitting = False
        with controller.operation_batch():
            controller.set_frequency("tx", latest_target)

        self.assertEqual(
            client.frequency_writes,
            [("MainA", 435_612_100), ("SubA", 145_912_300)],
        )
        self.assertEqual(client.ptt_reads, initial_ptt_reads + 4)

    def test_non_frequency_updates_remain_blocked_while_transmitting(self) -> None:
        client = FakeSharedClient()
        controller = SharedLocalRadioController(client, "MainA", "SubA", False)
        controller.initialize()
        client.transmitting = True

        with self.assertRaises(RadioOperationDeferred):
            with controller.operation_batch():
                controller.set_mode("rx", "USB", 2400)

        self.assertEqual(client.mode_writes, [])


if __name__ == "__main__":
    unittest.main()
