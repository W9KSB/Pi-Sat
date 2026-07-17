from __future__ import annotations

import unittest
from threading import Event, Thread

from pi_sat_controller.backend.rotator.rotator_manager import RotatorManager


class FakeRotatorClient:
    def __init__(self) -> None:
        self.commands: list[object] = []

    def set_position(self, azimuth_deg: float, elevation_deg: float) -> None:
        self.commands.append((azimuth_deg, elevation_deg))

    def stop(self) -> None:
        self.commands.append("S")

    def close(self) -> None:
        self.commands.append("q")


class BlockingRotatorClient(FakeRotatorClient):
    def __init__(self) -> None:
        super().__init__()
        self.write_started = Event()
        self.release_write = Event()

    def set_position(self, azimuth_deg: float, elevation_deg: float) -> None:
        self.write_started.set()
        self.release_write.wait(timeout=2.0)
        super().set_position(azimuth_deg, elevation_deg)


class RotatorManagerTests(unittest.TestCase):
    def test_shutdown_stops_and_quits_once_and_blocks_later_tracking_writes(self) -> None:
        client = FakeRotatorClient()
        manager = RotatorManager(client, enabled=True, write_enabled=True)

        manager.track_position(120.0, 30.0)
        manager.shutdown()
        manager.track_position(121.0, 31.0)
        manager.shutdown()

        self.assertEqual(client.commands, [(120.0, 30.0), "S", "q"])

    def test_shutdown_waits_for_in_flight_write_before_stop_and_quit(self) -> None:
        client = BlockingRotatorClient()
        manager = RotatorManager(client, enabled=True, write_enabled=True)
        tracking_thread = Thread(target=manager.track_position, args=(120.0, 30.0))
        shutdown_thread = Thread(target=manager.shutdown)

        tracking_thread.start()
        self.assertTrue(client.write_started.wait(timeout=1.0))
        shutdown_thread.start()
        self.assertEqual(client.commands, [])

        client.release_write.set()
        tracking_thread.join(timeout=1.0)
        shutdown_thread.join(timeout=1.0)

        self.assertFalse(tracking_thread.is_alive())
        self.assertFalse(shutdown_thread.is_alive())
        self.assertEqual(client.commands, [(120.0, 30.0), "S", "q"])


if __name__ == "__main__":
    unittest.main()
