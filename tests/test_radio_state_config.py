from __future__ import annotations

from pathlib import Path
import shutil
from tempfile import TemporaryDirectory
import unittest

from pi_sat_controller.backend.config import (
    load_cat_devices,
    load_settings,
    save_settings,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class RadioStateConfigTests(unittest.TestCase):
    def test_radio_state_update_preference_round_trips(self) -> None:
        with TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "pi-sat-controller.conf"
            shutil.copyfile(PROJECT_ROOT / "pi-sat-controller.conf.example", config_path)
            settings = load_settings(config_path)
            device = {
                "device_id": "test-radio",
                "name": "Test Radio",
                "connectivity": "local",
                "serial_port": "/dev/ttyUSB0",
                "baud": "115200",
                "model_id": "3081",
                "timeout_s": "2.0",
                "state_updates": "polling",
            }

            save_settings(
                settings,
                cat_devices=[device],
                path=config_path,
                validate_role_assignments=False,
            )
            saved = load_cat_devices(config_path)

            self.assertEqual(saved[0]["state_updates"], "polling")

    def test_missing_radio_state_update_preference_defaults_to_automatic(self) -> None:
        with TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "pi-sat-controller.conf"
            shutil.copyfile(PROJECT_ROOT / "pi-sat-controller.conf.example", config_path)
            settings = load_settings(config_path)
            device = {
                "device_id": "test-radio",
                "name": "Test Radio",
                "connectivity": "local",
                "serial_port": "/dev/ttyUSB0",
                "baud": "115200",
                "model_id": "3081",
                "timeout_s": "2.0",
            }

            save_settings(
                settings,
                cat_devices=[device],
                path=config_path,
                validate_role_assignments=False,
            )
            saved = load_cat_devices(config_path)

            self.assertEqual(saved[0]["state_updates"], "automatic")


if __name__ == "__main__":
    unittest.main()
