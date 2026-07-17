from __future__ import annotations

import unittest
from types import SimpleNamespace
from time import monotonic

try:
    import numpy as np
except ImportError:  # pragma: no cover - optional test dependency
    np = None

from pi_sat_controller.backend.controller.rx_tracking import RxTrackingManager
from pi_sat_controller.backend.models import SatelliteProfile, TransponderProfile
from pi_sat_controller.backend.orbital.orbital_engine import SatellitePosition


class FakeSdrManager:
    def __init__(self, frequency_hz: int | None) -> None:
        self.frequency_hz = frequency_hz
        self.error: str | None = None
        self.read_error: Exception | None = None
        self.writes: list[int] = []
        self.polls = 0
        self.reads = 0

    def snapshot(self):
        return self._state()

    def read_frequency_once(self):
        self.reads += 1
        if self.read_error is not None:
            raise self.read_error
        return self._state()

    def poll_once(self):
        self.polls += 1
        return self._state()

    def try_set_frequency(self, frequency_hz: int):
        self.frequency_hz = frequency_hz
        self.writes.append(frequency_hz)
        return self._state()

    def _state(self):
        return SimpleNamespace(frequency_hz=self.frequency_hz, error=self.error)


class FakeTxRadioManager:
    target_vfo = None
    restore_vfo_after_write = None
    split_mode_vfo = None

    def __init__(self, frequency_hz: int | None) -> None:
        self.frequency_hz = frequency_hz
        self.error: Exception | None = None
        self.writes: list[int] = []
        self.reads = 0

    def get_frequency(self) -> int:
        self.reads += 1
        if self.error is not None:
            raise self.error
        if self.frequency_hz is None:
            raise RuntimeError("TX frequency unavailable")
        return self.frequency_hz

    def try_set_frequency(self, frequency_hz: int, source: str = ""):
        self.frequency_hz = frequency_hz
        self.writes.append(frequency_hz)
        return SimpleNamespace(error=None)

    def set_vfo(self, vfo: str | None, source: str = ""):
        return SimpleNamespace(error=None)

    def set_mode(self, mode: str, passband_hz: int = 0, source: str = ""):
        return SimpleNamespace(error=None)


class RxTrackingManualReconciliationTests(unittest.TestCase):
    def test_rx_manual_readback_updates_offsets_before_writing_tx(self) -> None:
        sdr = FakeSdrManager(1_000_500)
        tx = FakeTxRadioManager(2_000_000)
        manager = make_manager(sdr, tx)
        seed_last_commanded(manager, rx_hz=1_000_000, tx_hz=2_000_000)

        manager._apply_update(make_position(), write_devices=True)
        snapshot = manager.snapshot()

        self.assertTrue(snapshot.pass_active)
        self.assertEqual(snapshot.user_downlink_offset_hz, 500)
        self.assertEqual(snapshot.mapped_user_uplink_offset_hz, 1_000)
        self.assertEqual(snapshot.target_rx_hz, 1_000_500)
        self.assertEqual(snapshot.calculated_tx_hz, 2_001_000)
        self.assertEqual(sdr.writes, [])
        self.assertEqual(tx.writes, [2_001_000])

    def test_tx_manual_readback_updates_offsets_before_writing_rx(self) -> None:
        sdr = FakeSdrManager(1_000_000)
        tx = FakeTxRadioManager(2_000_600)
        manager = make_manager(sdr, tx)
        seed_last_commanded(manager, rx_hz=1_000_000, tx_hz=2_000_000)

        manager._apply_update(make_position(), write_devices=True)
        snapshot = manager.snapshot()

        self.assertTrue(snapshot.pass_active)
        self.assertEqual(snapshot.user_downlink_offset_hz, 300)
        self.assertEqual(snapshot.mapped_user_uplink_offset_hz, 600)
        self.assertEqual(snapshot.target_rx_hz, 1_000_300)
        self.assertEqual(snapshot.calculated_tx_hz, 2_000_600)
        self.assertEqual(sdr.writes, [1_000_300])
        self.assertEqual(tx.writes, [])

    def test_simultaneous_rx_and_tx_manual_readbacks_disable_sync(self) -> None:
        sdr = FakeSdrManager(1_000_500)
        tx = FakeTxRadioManager(2_000_600)
        manager = make_manager(sdr, tx)
        seed_last_commanded(manager, rx_hz=1_000_000, tx_hz=2_000_000)

        manager._apply_update(make_position(), write_devices=True)
        snapshot = manager.snapshot()

        self.assertTrue(snapshot.pass_active)
        self.assertFalse(snapshot.sync_offsets)
        self.assertEqual(snapshot.user_downlink_offset_hz, 500)
        self.assertEqual(snapshot.mapped_user_uplink_offset_hz, 600)
        self.assertEqual(snapshot.target_rx_hz, 1_000_500)
        self.assertEqual(snapshot.calculated_tx_hz, 2_000_600)
        self.assertEqual(sdr.writes, [])
        self.assertEqual(tx.writes, [])
        self.assertIn("offset sync disabled", snapshot.error or "")

    def test_enabling_sync_captures_current_tx_alignment_without_stale_write(self) -> None:
        sdr = FakeSdrManager(1_000_000)
        tx = FakeTxRadioManager(2_000_600)
        manager = make_manager(sdr, tx)
        seed_last_commanded(manager, rx_hz=1_000_000, tx_hz=2_000_000)
        manager._sync_offsets = False

        snapshot = manager.set_offset_sync(True)

        self.assertTrue(snapshot.sync_offsets)
        self.assertEqual(snapshot.user_downlink_offset_hz, 0)
        self.assertEqual(snapshot.mapped_user_uplink_offset_hz, 600)
        self.assertEqual(snapshot.calculated_tx_hz, 2_000_600)
        self.assertEqual(tx.writes, [])

        snapshot = manager.adjust_downlink_offset(100)

        self.assertEqual(snapshot.user_downlink_offset_hz, 100)
        self.assertEqual(snapshot.mapped_user_uplink_offset_hz, 800)
        self.assertEqual(snapshot.calculated_tx_hz, 2_000_800)

    def test_enabling_sync_locks_inverted_profile_from_current_alignment(self) -> None:
        sdr = FakeSdrManager(1_000_000)
        tx = FakeTxRadioManager(1_999_400)
        manager = make_manager(sdr, tx, inverted=True, ratio=1.0)
        seed_last_commanded(manager, rx_hz=1_000_000, tx_hz=2_000_000)
        manager._sync_offsets = False

        snapshot = manager.set_offset_sync(True)

        self.assertTrue(snapshot.sync_offsets)
        self.assertEqual(snapshot.user_downlink_offset_hz, 0)
        self.assertEqual(snapshot.mapped_user_uplink_offset_hz, -600)
        self.assertEqual(snapshot.calculated_tx_hz, 1_999_400)
        self.assertEqual(tx.writes, [])

        snapshot = manager.adjust_downlink_offset(100)

        self.assertEqual(snapshot.user_downlink_offset_hz, 100)
        self.assertEqual(snapshot.mapped_user_uplink_offset_hz, -700)
        self.assertEqual(snapshot.target_rx_hz, 1_000_100)
        self.assertEqual(snapshot.calculated_tx_hz, 1_999_300)

    def test_enabling_sync_is_idempotent_after_baseline_capture(self) -> None:
        sdr = FakeSdrManager(1_000_000)
        tx = FakeTxRadioManager(1_999_400)
        manager = make_manager(sdr, tx, inverted=True, ratio=1.0)
        seed_last_commanded(manager, rx_hz=1_000_000, tx_hz=2_000_000)
        manager._sync_offsets = False

        baseline = manager.set_offset_sync(True)
        tx_reads_after_baseline = tx.reads
        sdr_reads_after_baseline = sdr.reads

        sdr.frequency_hz = 1_000_050
        tx.frequency_hz = 1_999_300
        snapshot = manager.set_offset_sync(True)

        self.assertTrue(baseline.sync_offsets)
        self.assertTrue(snapshot.sync_offsets)
        self.assertEqual(snapshot.user_downlink_offset_hz, 0)
        self.assertEqual(snapshot.mapped_user_uplink_offset_hz, -600)
        self.assertEqual(snapshot.calculated_tx_hz, 1_999_400)
        self.assertEqual(tx.reads, tx_reads_after_baseline)
        self.assertEqual(sdr.reads, sdr_reads_after_baseline)

    def test_enabling_sync_requires_tx_readback_during_active_pass(self) -> None:
        sdr = FakeSdrManager(1_000_000)
        tx = FakeTxRadioManager(2_000_600)
        tx.error = RuntimeError("PTT active")
        manager = make_manager(sdr, tx)
        seed_last_commanded(manager, rx_hz=1_000_000, tx_hz=2_000_000)
        manager._sync_offsets = False

        snapshot = manager.set_offset_sync(True)

        self.assertFalse(snapshot.sync_offsets)
        self.assertIn("Cannot enable RX/TX offset sync", snapshot.error or "")
        self.assertEqual(tx.writes, [])

    def test_failed_tx_read_skips_tx_write_and_preserves_offsets(self) -> None:
        sdr = FakeSdrManager(1_000_000)
        tx = FakeTxRadioManager(2_000_000)
        tx.error = RuntimeError("PTT active")
        manager = make_manager(sdr, tx)
        seed_last_commanded(manager, rx_hz=1_000_000, tx_hz=2_000_000)

        manager._apply_update(make_position(range_rate_m_s=1_000.0), write_devices=True)
        snapshot = manager.snapshot()

        self.assertTrue(snapshot.pass_active)
        self.assertEqual(snapshot.user_downlink_offset_hz, 0)
        self.assertEqual(snapshot.mapped_user_uplink_offset_hz, 0)
        self.assertEqual(tx.writes, [])
        self.assertIn("Skipped TX manual readback check", snapshot.error or "")

    def test_recent_rx_command_does_not_block_live_manual_readback(self) -> None:
        sdr = FakeSdrManager(1_000_500)
        tx = FakeTxRadioManager(2_000_000)
        manager = make_manager(sdr, tx)
        seed_last_commanded(manager, rx_hz=1_000_000, tx_hz=2_000_000)
        manager._last_commanded_at = monotonic()

        manager._apply_update(make_position(), write_devices=True)
        snapshot = manager.snapshot()

        self.assertTrue(snapshot.pass_active)
        self.assertEqual(snapshot.user_downlink_offset_hz, 500)
        self.assertEqual(snapshot.target_rx_hz, 1_000_500)
        self.assertEqual(sdr.writes, [])

    def test_failed_strict_rx_read_does_not_write_stale_target(self) -> None:
        sdr = FakeSdrManager(1_000_000)
        sdr.read_error = RuntimeError("rigctl read timeout")
        tx = FakeTxRadioManager(2_000_000)
        manager = make_manager(sdr, tx)
        seed_last_commanded(manager, rx_hz=1_000_000, tx_hz=2_000_000)

        manager._apply_update(make_position(range_rate_m_s=1_000.0), write_devices=True)
        snapshot = manager.snapshot()

        self.assertTrue(snapshot.pass_active)
        self.assertEqual(snapshot.user_downlink_offset_hz, 0)
        self.assertEqual(sdr.writes, [])
        self.assertIn("Skipped RX manual readback check", snapshot.error or "")

    def test_off_pass_rx_readback_is_not_treated_as_manual_offset(self) -> None:
        sdr = FakeSdrManager(1_000_500)
        tx = FakeTxRadioManager(2_000_000)
        manager = make_manager(sdr, tx)
        seed_last_commanded(manager, rx_hz=1_000_000, tx_hz=2_000_000)

        manager._apply_update(make_position(elevation_deg=-1.0), write_devices=True)
        snapshot = manager.snapshot()

        self.assertFalse(snapshot.pass_active)
        self.assertEqual(snapshot.user_downlink_offset_hz, 0)
        self.assertEqual(snapshot.target_rx_hz, 1_000_000)
        self.assertEqual(sdr.writes, [1_000_000])

    def test_off_pass_tx_readback_is_not_treated_as_manual_offset(self) -> None:
        sdr = FakeSdrManager(1_000_000)
        tx = FakeTxRadioManager(2_000_600)
        manager = make_manager(sdr, tx)
        seed_last_commanded(manager, rx_hz=1_000_000, tx_hz=2_000_000)

        manager._apply_update(make_position(elevation_deg=-1.0), write_devices=True)
        snapshot = manager.snapshot()

        self.assertFalse(snapshot.pass_active)
        self.assertEqual(snapshot.user_downlink_offset_hz, 0)
        self.assertEqual(snapshot.mapped_user_uplink_offset_hz, 0)
        self.assertEqual(snapshot.calculated_tx_hz, 2_000_000)
        self.assertEqual(tx.writes, [2_000_000])

    def test_refresh_snapshot_only_updates_doppler_when_tracking_is_off(self) -> None:
        sdr = FakeSdrManager(1_000_000)
        tx = FakeTxRadioManager(2_000_000)
        orbital_engine = FakeOrbitalEngine(make_position(range_rate_m_s=1_000.0, elevation_deg=-1.0))
        manager = make_manager(sdr, tx, orbital_engine=orbital_engine)

        manager.refresh_snapshot_only()
        snapshot = manager.snapshot()

        self.assertFalse(snapshot.active)
        self.assertFalse(snapshot.pass_active)
        self.assertNotEqual(snapshot.downlink_doppler_hz, 0)
        self.assertEqual(
            snapshot.target_rx_hz,
            manager.transponder.preferred_downlink + snapshot.downlink_doppler_hz,
        )

    def test_update_runtime_dependencies_preserves_tracking_state(self) -> None:
        sdr = FakeSdrManager(1_000_000)
        tx = FakeTxRadioManager(2_000_000)
        manager = make_manager(sdr, tx, orbital_engine=FakeOrbitalEngine(make_position()))
        manager._active = True
        manager._user_downlink_offset_hz = 250
        manager._user_uplink_offset_hz = 500
        manager._sync_offsets = False
        manager._rx_session_ready = True
        manager._tx_session_ready = True

        replacement_sdr = FakeSdrManager(1_000_250)
        replacement_tx = FakeTxRadioManager(2_000_500)

        manager.update_runtime_dependencies(
            sdr_manager=replacement_sdr,
            tx_radio_manager=replacement_tx,
            rotator_manager=None,
        )

        self.assertIs(manager.sdr_manager, replacement_sdr)
        self.assertIs(manager.tx_radio_manager, replacement_tx)
        self.assertEqual(manager._user_downlink_offset_hz, 250)
        self.assertEqual(manager._user_uplink_offset_hz, 500)
        self.assertFalse(manager._sync_offsets)
        self.assertFalse(manager._rx_session_ready)
        self.assertFalse(manager._tx_session_ready)

    @unittest.skipIf(np is None, "numpy not installed")
    def test_refresh_snapshot_only_normalizes_numpy_scalars(self) -> None:
        sdr = FakeSdrManager(1_000_000)
        tx = FakeTxRadioManager(2_000_000)
        orbital_engine = FakeOrbitalEngine(
            SatellitePosition(
                azimuth_deg=np.float64(180.0),
                elevation_deg=np.float64(1.25),
                latitude_deg=np.float64(10.5),
                longitude_deg=np.float64(-20.5),
                range_km=np.float64(1234.5),
                range_rate_m_s=np.float64(432.1),
            )
        )
        manager = make_manager(sdr, tx, orbital_engine=orbital_engine)

        manager.refresh_snapshot_only()
        snapshot = manager.snapshot()

        self.assertIs(type(snapshot.pass_active), bool)
        self.assertIs(type(snapshot.sync_offsets), bool)
        self.assertIs(type(snapshot.azimuth_deg), float)
        self.assertIs(type(snapshot.target_rx_hz), int)


def make_manager(
    sdr: FakeSdrManager,
    tx: FakeTxRadioManager,
    orbital_engine=None,
    *,
    inverted: bool = False,
    ratio: float = 2.0,
) -> RxTrackingManager:
    return RxTrackingManager(
        orbital_engine=orbital_engine or SimpleNamespace(),
        sdr_manager=sdr,
        satellite=SatelliteProfile(name="TESTSAT", norad_id=12345, favorite=True, transponders=[]),
        transponder=TransponderProfile(
            name="Linear",
            type="linear",
            uplink_low=2_000_000,
            uplink_high=2_100_000,
            downlink_low=1_000_000,
            downlink_high=1_100_000,
            uplink_mode="USB",
            downlink_mode="USB",
            inverted=inverted,
            ratio=ratio,
            preferred_uplink=2_000_000,
            preferred_downlink=1_000_000,
        ),
        deadband_hz=10,
        tx_radio_manager=tx,
    )


class FakeOrbitalEngine:
    def __init__(self, position: SatellitePosition) -> None:
        self.position = position

    def get_position(self, _norad_id: int) -> SatellitePosition:
        return self.position


def seed_last_commanded(
    manager: RxTrackingManager,
    rx_hz: int,
    tx_hz: int,
) -> None:
    manager._active = True
    manager._last_snapshot = manager._last_snapshot.__class__(
        **{
            **manager._last_snapshot.__dict__,
            "active": True,
            "pass_active": True,
            "downlink_doppler_hz": 0,
            "uplink_doppler_hz": 0,
        }
    )
    manager._last_commanded_rx_hz = rx_hz
    manager._last_commanded_tx_hz = tx_hz
    manager._last_commanded_at = 0.0


def make_position(
    range_rate_m_s: float = 0.0,
    elevation_deg: float = 30.0,
) -> SatellitePosition:
    return SatellitePosition(
        azimuth_deg=180.0,
        elevation_deg=elevation_deg,
        latitude_deg=0.0,
        longitude_deg=0.0,
        range_km=1_000.0,
        range_rate_m_s=range_rate_m_s,
    )


if __name__ == "__main__":
    unittest.main()
