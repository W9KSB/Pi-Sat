from pathlib import Path
import shutil
from tempfile import TemporaryDirectory
import unittest

from pi_sat_controller.backend.config import (
    PROJECT_ROOT,
    load_my_satellites,
    save_my_satellites,
)
from pi_sat_controller.backend.models import MySatellite


class AutotrackFilterConfigTests(unittest.TestCase):
    def setUp(self) -> None:
        self.satellites = [
            MySatellite(norad_id=100, name="Alpha"),
            MySatellite(norad_id=200, name="Bravo"),
        ]

    def test_missing_filter_defaults_to_all_configured_satellites(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "pi-sat-controller.conf"
            path.write_text(
                "[my_satellites]\n"
                "min_pass_elevation_deg = 10\n"
                "autotrack_next_pass = true\n"
                "satellite_100 = Alpha\n"
                "satellite_200 = Bravo\n",
                encoding="utf-8",
            )

            _, _, _, autotrack_norads = load_my_satellites(path)

            self.assertEqual(autotrack_norads, {100, 200})

    def test_saved_filter_survives_reload(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "pi-sat-controller.conf"
            shutil.copyfile(PROJECT_ROOT / "pi-sat-controller.conf.example", path)

            save_my_satellites(self.satellites, 10.0, True, {200}, path)
            _, _, _, autotrack_norads = load_my_satellites(path)

            self.assertEqual(autotrack_norads, {200})

    def test_empty_saved_filter_remains_empty(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "pi-sat-controller.conf"
            shutil.copyfile(PROJECT_ROOT / "pi-sat-controller.conf.example", path)

            save_my_satellites(self.satellites, 10.0, True, set(), path)
            _, _, _, autotrack_norads = load_my_satellites(path)

            self.assertEqual(autotrack_norads, set())


if __name__ == "__main__":
    unittest.main()
