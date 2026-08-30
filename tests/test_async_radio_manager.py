from __future__ import annotations

from collections import deque
from time import monotonic
import unittest

from pi_sat_controller.backend.radio.radio_manager import RadioManager
from pi_sat_controller.backend.radio.radio_state import (
    ASYNC_RECONCILIATION_MISS_THRESHOLD,
    ASYNC_RECONCILIATION_POLL_S,
    RadioStateClassification,
    RadioStateEvent,
    RadioStateProperty,
)


class FakeRadioClient:
    def __init__(self, frequency_hz: int = 435_000_000) -> None:
        self.frequency_hz = frequency_hz
        self.get_count = 0
        self.verified = False
        self.listener_running = True
        self.events: deque[RadioStateEvent] = deque()
        self.generation = 1
        self.unverify_reasons: list[str] = []

    def get_frequency(self) -> int:
        self.get_count += 1
        return self.frequency_hz

    def set_frequency(self, frequency_hz: int) -> None:
        self.frequency_hz = frequency_hz

    def set_frequency_on_vfo(self, _vfo, frequency_hz: int) -> None:
        self.set_frequency(frequency_hz)

    def drain_radio_state_events(self):
        events = list(self.events)
        self.events.clear()
        return events

    def is_async_property_verified(self, property) -> bool:
        return (
            self.verified
            and self.listener_running
            and RadioStateProperty(property) == RadioStateProperty.FREQUENCY
        )

    def mark_async_property_unverified(self, property, *, reason: str) -> None:
        if RadioStateProperty(property) == RadioStateProperty.FREQUENCY:
            self.verified = False
            self.unverify_reasons.append(reason)

    def async_status(self):
        return {
            "preference": "automatic",
            "state": "verified" if self.verified else "available",
            "available": self.listener_running,
            "healthy": self.listener_running,
            "verified_properties": ["frequency"] if self.verified else [],
        }

    def ensure_connected(self) -> int:
        return self.generation


def event(value: int, classification: RadioStateClassification) -> RadioStateEvent:
    return RadioStateEvent(
        property=RadioStateProperty.FREQUENCY,
        value=value,
        role="rx",
        vfo="MainA",
        timestamp=monotonic(),
        classification=classification,
    )


def ambiguous_event(value: int) -> RadioStateEvent:
    return RadioStateEvent(
        property=RadioStateProperty.FREQUENCY,
        value=value,
        role="rx",
        vfo="VFOA",
        timestamp=monotonic(),
        classification=RadioStateClassification.STATE_REFRESH,
        requires_reconciliation=True,
    )


class AsyncRadioManagerTests(unittest.TestCase):
    def make_manager(self, client: FakeRadioClient) -> RadioManager:
        return RadioManager(
            client=client,
            enabled=True,
            write_enabled=True,
            target_vfo="MainA",
            poll_target_vfo=False,
        )

    def test_no_events_keeps_normal_polling_until_verified(self) -> None:
        client = FakeRadioClient()
        manager = self.make_manager(client)

        first = manager.get_frequency_for_reconciliation()
        second = manager.get_frequency_for_reconciliation()

        self.assertTrue(first.from_poll)
        self.assertTrue(second.from_poll)
        self.assertEqual(client.get_count, 2)

    def test_verified_event_backs_off_frequency_polling(self) -> None:
        client = FakeRadioClient()
        manager = self.make_manager(client)
        manager.get_frequency_for_reconciliation()
        client.verified = True
        client.events.append(event(435_001_000, RadioStateClassification.EXTERNAL_CHANGE))

        observation = manager.get_frequency_for_reconciliation()

        self.assertFalse(observation.from_poll)
        self.assertEqual(observation.classification, RadioStateClassification.EXTERNAL_CHANGE)
        self.assertEqual(observation.frequency_hz, 435_001_000)
        self.assertEqual(client.get_count, 1)

    def test_self_echo_updates_observed_state_without_polling(self) -> None:
        client = FakeRadioClient()
        manager = self.make_manager(client)
        manager.get_frequency_for_reconciliation()
        client.verified = True
        client.events.append(event(435_000_100, RadioStateClassification.SELF_ECHO))

        observation = manager.get_frequency_for_reconciliation()

        self.assertEqual(observation.classification, RadioStateClassification.SELF_ECHO)
        self.assertEqual(manager.snapshot().frequency_hz, 435_000_100)
        self.assertEqual(client.get_count, 1)

    def test_slow_reconciliation_poll_remains_enabled(self) -> None:
        client = FakeRadioClient()
        manager = self.make_manager(client)
        manager.get_frequency_for_reconciliation()
        client.verified = True
        manager._last_reconciliation_poll_at = monotonic() - ASYNC_RECONCILIATION_POLL_S - 0.1

        observation = manager.get_frequency_for_reconciliation()

        self.assertTrue(observation.from_poll)
        self.assertEqual(client.get_count, 2)
        self.assertTrue(client.verified)
        self.assertEqual(client.unverify_reasons, [])

    def test_single_missed_manual_change_is_recovered_without_fallback(self) -> None:
        client = FakeRadioClient()
        manager = self.make_manager(client)
        manager.get_frequency_for_reconciliation()
        client.verified = True
        client.frequency_hz = 435_001_000
        manager._last_reconciliation_poll_at = (
            monotonic() - ASYNC_RECONCILIATION_POLL_S - 0.1
        )

        missed_change = manager.get_frequency_for_reconciliation()
        next_observation = manager.get_frequency_for_reconciliation()

        self.assertTrue(missed_change.from_poll)
        self.assertEqual(missed_change.frequency_hz, 435_001_000)
        self.assertEqual(
            missed_change.classification,
            RadioStateClassification.EXTERNAL_CHANGE,
        )
        self.assertTrue(client.verified)
        self.assertEqual(client.unverify_reasons, [])
        self.assertFalse(next_observation.from_poll)
        self.assertEqual(next_observation.frequency_hz, 435_001_000)
        self.assertEqual(client.get_count, 2)

    def test_intermediate_push_then_final_polled_value_keeps_async_verified(self) -> None:
        client = FakeRadioClient(436_812_500)
        manager = self.make_manager(client)
        manager.get_frequency_for_reconciliation()
        client.verified = True
        client.events.append(
            event(436_751_500, RadioStateClassification.EXTERNAL_CHANGE)
        )

        pushed = manager.get_frequency_for_reconciliation()
        client.frequency_hz = 436_750_000
        manager._last_reconciliation_poll_at = (
            monotonic() - ASYNC_RECONCILIATION_POLL_S - 0.1
        )
        settled = manager.get_frequency_for_reconciliation()

        self.assertEqual(pushed.frequency_hz, 436_751_500)
        self.assertFalse(pushed.from_poll)
        self.assertEqual(settled.frequency_hz, 436_750_000)
        self.assertTrue(settled.from_poll)
        self.assertEqual(
            settled.classification,
            RadioStateClassification.EXTERNAL_CHANGE,
        )
        self.assertTrue(client.verified)
        self.assertEqual(client.unverify_reasons, [])

    def test_repeated_reconciliation_misses_restore_normal_polling(self) -> None:
        client = FakeRadioClient()
        manager = self.make_manager(client)
        manager.get_frequency_for_reconciliation()
        client.verified = True

        for _ in range(ASYNC_RECONCILIATION_MISS_THRESHOLD):
            client.frequency_hz += 1_000
            manager._last_reconciliation_poll_at = (
                monotonic() - ASYNC_RECONCILIATION_POLL_S - 0.1
            )
            observation = manager.get_frequency_for_reconciliation()
            self.assertTrue(observation.from_poll)
            self.assertEqual(
                observation.classification,
                RadioStateClassification.EXTERNAL_CHANGE,
            )

        self.assertFalse(client.verified)
        self.assertEqual(len(client.unverify_reasons), 1)
        self.assertIn("repeated_reconciliation_mismatch", client.unverify_reasons[0])
        self.assertIn(
            f"count={ASYNC_RECONCILIATION_MISS_THRESHOLD}",
            client.unverify_reasons[0],
        )
        self.assertTrue(manager.get_frequency_for_reconciliation().from_poll)

    def test_matching_reconciliation_resets_miss_count(self) -> None:
        client = FakeRadioClient()
        manager = self.make_manager(client)
        manager.get_frequency_for_reconciliation()
        client.verified = True

        client.frequency_hz += 1_000
        manager._last_reconciliation_poll_at = (
            monotonic() - ASYNC_RECONCILIATION_POLL_S - 0.1
        )
        manager.get_frequency_for_reconciliation()
        manager._last_reconciliation_poll_at = (
            monotonic() - ASYNC_RECONCILIATION_POLL_S - 0.1
        )
        manager.get_frequency_for_reconciliation()

        for _ in range(ASYNC_RECONCILIATION_MISS_THRESHOLD - 1):
            client.frequency_hz += 1_000
            manager._last_reconciliation_poll_at = (
                monotonic() - ASYNC_RECONCILIATION_POLL_S - 0.1
            )
            manager.get_frequency_for_reconciliation()

        self.assertTrue(client.verified)
        self.assertEqual(client.unverify_reasons, [])

    def test_listener_failure_resumes_normal_polling(self) -> None:
        client = FakeRadioClient()
        manager = self.make_manager(client)
        manager.get_frequency_for_reconciliation()
        client.verified = True
        manager.get_frequency_for_reconciliation()
        client.listener_running = False

        observation = manager.get_frequency_for_reconciliation()

        self.assertTrue(observation.from_poll)
        self.assertEqual(client.get_count, 2)

    def test_ambiguous_event_does_not_overwrite_role_cache(self) -> None:
        client = FakeRadioClient()
        manager = self.make_manager(client)
        manager.get_frequency_for_reconciliation()
        client.verified = True
        client.events.append(ambiguous_event(145_965_995))

        observation = manager.get_frequency_for_reconciliation()

        self.assertTrue(observation.from_poll)
        self.assertEqual(observation.frequency_hz, 435_000_000)
        self.assertEqual(manager.snapshot().frequency_hz, 435_000_000)
        self.assertTrue(client.verified)
        self.assertEqual(client.unverify_reasons, [])

    def test_connection_generation_change_forces_reconciliation(self) -> None:
        client = FakeRadioClient()
        manager = self.make_manager(client)
        manager.connection_generation()
        manager.get_frequency_for_reconciliation()
        client.verified = True
        client.generation += 1

        manager.connection_generation()
        observation = manager.get_frequency_for_reconciliation()

        self.assertTrue(observation.from_poll)


if __name__ == "__main__":
    unittest.main()
