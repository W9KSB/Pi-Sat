from __future__ import annotations

from datetime import datetime, timedelta, timezone
import logging
import unittest

from pi_sat_controller.backend.controller.autotrack import AutotrackCoordinator
from pi_sat_controller.backend.models import SatellitePass


NOW = datetime(2026, 7, 15, 12, 0, tzinfo=timezone.utc)


def make_pass(
    norad_id: int,
    *,
    aos_offset_s: int,
    los_offset_s: int,
) -> SatellitePass:
    return SatellitePass(
        satellite_name=f"SAT-{norad_id}",
        norad_id=norad_id,
        aos_utc=NOW + timedelta(seconds=aos_offset_s),
        max_utc=NOW + timedelta(seconds=(aos_offset_s + los_offset_s) / 2),
        los_utc=NOW + timedelta(seconds=los_offset_s),
        start_azimuth_deg=100.0,
        middle_azimuth_deg=180.0,
        end_azimuth_deg=260.0,
        max_elevation_deg=45.0,
    )


class AutotrackCoordinatorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.enabled = True
        self.configured_norads = {100, 200}
        self.passes: list[SatellitePass] = []
        self.started: list[int] = []
        self.coordinator = AutotrackCoordinator(
            load_options=lambda: (set(self.configured_norads), self.enabled),
            get_passes=lambda: list(self.passes),
            start_pass=lambda satellite_pass: self.started.append(satellite_pass.norad_id),
            logger=logging.getLogger(__name__),
            retry_interval_s=30.0,
        )

    def test_selects_the_first_upcoming_pass_before_aos(self) -> None:
        self.passes = [make_pass(100, aos_offset_s=1, los_offset_s=60)]

        self.assertTrue(self.coordinator.tick(NOW))
        self.assertEqual(self.started, [100])

    def test_starts_an_active_pass_only_once(self) -> None:
        self.passes = [make_pass(100, aos_offset_s=-10, los_offset_s=60)]

        self.assertTrue(self.coordinator.tick(NOW))
        self.assertFalse(self.coordinator.tick(NOW + timedelta(seconds=1)))
        self.assertEqual(self.started, [100])

    def test_reset_reselects_first_pass_after_autotrack_is_reenabled(self) -> None:
        self.passes = [make_pass(100, aos_offset_s=60, los_offset_s=120)]

        self.assertTrue(self.coordinator.tick(NOW))
        self.coordinator.reset()
        self.assertTrue(self.coordinator.tick(NOW))
        self.assertEqual(self.started, [100, 100])

    def test_ignores_unconfigured_satellites(self) -> None:
        self.passes = [make_pass(300, aos_offset_s=-10, los_offset_s=60)]

        self.assertFalse(self.coordinator.tick(NOW))
        self.assertEqual(self.started, [])

    def test_disabled_autotrack_never_starts_a_pass(self) -> None:
        self.enabled = False
        self.passes = [make_pass(100, aos_offset_s=-10, los_offset_s=60)]

        self.assertFalse(self.coordinator.tick(NOW))
        self.assertEqual(self.started, [])

    def test_cancelled_start_is_not_marked_as_handled(self) -> None:
        attempts: list[int] = []

        def cancel_start(satellite_pass: SatellitePass) -> bool:
            attempts.append(satellite_pass.norad_id)
            return False

        self.passes = [make_pass(100, aos_offset_s=-10, los_offset_s=120)]
        coordinator = AutotrackCoordinator(
            load_options=lambda: ({100}, True),
            get_passes=lambda: list(self.passes),
            start_pass=cancel_start,
            logger=logging.getLogger(__name__),
            retry_interval_s=1.0,
        )

        self.assertFalse(coordinator.tick(NOW))
        self.assertFalse(coordinator.tick(NOW + timedelta(seconds=1)))
        self.assertEqual(attempts, [100, 100])

    def test_earliest_aos_wins_and_next_pass_is_selected_after_los(self) -> None:
        self.passes = [
            make_pass(200, aos_offset_s=-5, los_offset_s=60),
            make_pass(100, aos_offset_s=-20, los_offset_s=30),
        ]

        self.assertTrue(self.coordinator.tick(NOW))
        self.assertEqual(self.started, [100])

        self.assertTrue(self.coordinator.tick(NOW + timedelta(seconds=31)))
        self.assertEqual(self.started, [100, 200])

    def test_failed_start_retries_after_cooldown(self) -> None:
        attempts: list[int] = []

        def fail_start(satellite_pass: SatellitePass) -> None:
            attempts.append(satellite_pass.norad_id)
            raise RuntimeError("temporary failure")

        self.passes = [make_pass(100, aos_offset_s=-10, los_offset_s=120)]
        coordinator = AutotrackCoordinator(
            load_options=lambda: ({100}, True),
            get_passes=lambda: list(self.passes),
            start_pass=fail_start,
            logger=logging.getLogger(f"{__name__}.expected_failure"),
            retry_interval_s=30.0,
        )
        logging.getLogger(f"{__name__}.expected_failure").disabled = True

        self.assertFalse(coordinator.tick(NOW))
        self.assertFalse(coordinator.tick(NOW + timedelta(seconds=29)))
        self.assertFalse(coordinator.tick(NOW + timedelta(seconds=30)))
        self.assertEqual(attempts, [100, 100])


if __name__ == "__main__":
    unittest.main()
