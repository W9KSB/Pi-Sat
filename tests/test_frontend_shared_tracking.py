from __future__ import annotations

import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
APP_JS = PROJECT_ROOT / "frontend" / "app.js"


class FrontendSharedTrackingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.source = APP_JS.read_text(encoding="utf-8")

    def test_browser_has_no_autotrack_decision_loop(self) -> None:
        self.assertNotIn("checkAutotrackNextPass", self.source)
        self.assertNotIn("activeAutotrackPassKey", self.source)

    def test_polling_does_not_restart_a_stale_local_selection(self) -> None:
        self.assertNotIn("shouldBootstrapTracking", self.source)
        self.assertNotIn(
            "const started = await syncTrackingForSelection",
            self.source,
        )

    def test_backend_target_is_applied_to_the_browser_selection(self) -> None:
        self.assertIn(
            "sharedTrackingSelectionDiffers(result)",
            self.source,
        )
        self.assertIn("restoreSelectionFromTracking();", self.source)

    def test_backend_profile_index_is_applied_for_same_satellite(self) -> None:
        self.assertIn(
            "return sharedProfileIndex !== selectedFrequencyProfileIndex;",
            self.source,
        )
        self.assertIn(
            "latestTracking.frequency_profile_index",
            self.source,
        )

    def test_offset_sync_toggle_is_applied_from_backend_polling(self) -> None:
        self.assertIn("syncRxTx = result.sync_offsets;", self.source)
        self.assertIn("syncToggle.checked = result.sync_offsets;", self.source)

    def test_device_control_updates_only_the_changed_toggle(self) -> None:
        self.assertIn(
            "JSON.stringify({ [payloadKey]: requestedEnabled })",
            self.source,
        )
        self.assertIn("setInterval(loadStatus, 2000);", self.source)

    def test_device_toggle_clears_focus_before_waiting_for_backend(self) -> None:
        handler = self.source[
            self.source.index("async function updateDeviceControl(event)")
            : self.source.index("async function refreshTleData()")
        ]

        self.assertLess(handler.index("toggle.blur();"), handler.index("await fetch("))
        self.assertIn("toggle.disabled = true;", handler)
        self.assertIn("toggle.disabled = false;", handler)
        self.assertIn(
            ".getElementById('rotator-control-toggle')\n  .addEventListener('input', updateDeviceControl);",
            self.source,
        )

    def test_startup_restores_filter_before_loading_passes(self) -> None:
        startup = self.source[
            self.source.index("async function initializePassControls()"):
        ]

        self.assertLess(
            startup.index("await loadMySatellites();"),
            startup.index("await loadPasses();"),
        )
        self.assertNotIn(
            "\nloadMySatellites();\ninitializePassControls();",
            self.source,
        )


if __name__ == "__main__":
    unittest.main()
