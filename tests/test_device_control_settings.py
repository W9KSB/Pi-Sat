from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from pi_sat_controller.backend.config import load_config, load_settings, save_settings


PROJECT_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE_CONFIG = PROJECT_ROOT / "pi-sat-controller.conf.example"


class DeviceControlSettingsTests(unittest.TestCase):
    def test_write_enabled_is_no_longer_exposed_or_persisted(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "pi-sat-controller.conf"
            legacy_text = EXAMPLE_CONFIG.read_text(encoding="utf-8")
            legacy_text = legacy_text.replace(
                "cat_debug_logging = false\n\n[tx]",
                "cat_debug_logging = false\nwrite_enabled = false\n\n[tx]",
            )
            legacy_text = legacy_text.replace(
                "cat_debug_logging = false\n\n[rotator]",
                "cat_debug_logging = false\nwrite_enabled = false\n\n[rotator]",
            )
            legacy_text = legacy_text.replace(
                "cat_debug_logging = \ntimeout_s = 2.0",
                "cat_debug_logging = \nwrite_enabled = false\ntimeout_s = 2.0",
            )
            config_path.write_text(legacy_text, encoding="utf-8")

            settings = load_settings(config_path)
            self.assertNotIn("write_enabled", settings["rx"])
            self.assertNotIn("write_enabled", settings["tx"])
            self.assertNotIn("write_enabled", settings["rotator"])

            config = load_config(config_path)
            self.assertTrue(config.rx.write_enabled)
            self.assertTrue(config.tx.write_enabled)
            self.assertTrue(config.rotator.write_enabled)

            save_settings({}, path=config_path)

            rendered = config_path.read_text(encoding="utf-8")
            self.assertNotIn("write_enabled =", rendered)


if __name__ == "__main__":
    unittest.main()
