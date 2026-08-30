from __future__ import annotations

import json
import socket
from threading import Event
from time import monotonic
from types import SimpleNamespace
import unittest
from unittest.mock import call, patch

from pi_sat_controller.backend.radio.hamlib_async import (
    HamlibAsyncStateListener,
    parse_hamlib_version,
    probe_hamlib_async_capability,
)
from pi_sat_controller.backend.radio.local_hamlib_client import (
    LocalHamlibClient,
    _build_rigctld_command,
)
from pi_sat_controller.backend.radio.radio_state import (
    AsyncCapabilityState,
    RecentCommandHistory,
    RadioStateClassification,
    RadioStateEvent,
    RadioStateProperty,
)


def frequency_event(
    value: int,
    *,
    role: str | None = "rx",
    vfo: str | None = "MainA",
    timestamp: float = 1.0,
) -> RadioStateEvent:
    return RadioStateEvent(
        property=RadioStateProperty.FREQUENCY,
        value=value,
        role=role,
        vfo=vfo,
        timestamp=timestamp,
        classification=RadioStateClassification.EXTERNAL_CHANGE,
    )


class RecentCommandHistoryTests(unittest.TestCase):
    def test_out_of_order_echo_matches_non_latest_command(self) -> None:
        history = RecentCommandHistory(window_s=2.0)
        history.record(RadioStateProperty.FREQUENCY, 435_612_200, "rx", "MainA", sent_at=1.0)
        history.record(RadioStateProperty.FREQUENCY, 435_612_300, "rx", "MainA", sent_at=1.1)
        history.record(RadioStateProperty.FREQUENCY, 435_612_400, "rx", "MainA", sent_at=1.2)

        match = history.match(frequency_event(435_612_300, timestamp=1.3), now=1.3)

        self.assertIsNotNone(match)
        self.assertEqual(match.value, 435_612_300)

    def test_expired_command_does_not_hide_manual_return_to_same_frequency(self) -> None:
        history = RecentCommandHistory(window_s=2.0)
        history.record(RadioStateProperty.FREQUENCY, 435_612_000, "rx", "MainA", sent_at=1.0)

        self.assertIsNone(
            history.match(frequency_event(435_612_000, timestamp=11.0), now=11.0)
        )

    def test_rx_tx_routes_are_not_cross_matched(self) -> None:
        history = RecentCommandHistory(window_s=2.0)
        history.record(RadioStateProperty.FREQUENCY, 145_900_000, "rx", "MainA", sent_at=1.0)
        history.record(RadioStateProperty.FREQUENCY, 145_900_000, "tx", "SubA", sent_at=1.0)

        rx_match = history.match(
            frequency_event(145_900_000, role="rx", vfo="MainA"),
            now=1.1,
        )
        wrong_route = history.match(
            frequency_event(145_900_000, role="rx", vfo="MainA"),
            now=1.2,
        )
        tx_match = history.match(
            frequency_event(145_900_000, role="tx", vfo="SubA"),
            now=1.2,
        )

        self.assertEqual(rx_match.role, "rx")
        self.assertIsNone(wrong_route)
        self.assertEqual(tx_match.role, "tx")


class HamlibAsyncListenerTests(unittest.TestCase):
    def test_loopback_udp_listener_receives_changed_snapshot(self) -> None:
        received: list[RadioStateEvent] = []
        received_event = Event()

        def collect(event: RadioStateEvent) -> None:
            received.append(event)
            received_event.set()

        listener = HamlibAsyncStateListener(collect)
        listener.seed_observed(RadioStateProperty.FREQUENCY, 435_612_000, "MainA")
        port = listener.start()
        payload = json.dumps(
            {
                "app": "Hamlib",
                "version": "4.7.2",
                "seq": 3,
                "vfos": [
                    {
                        "name": "MainA",
                        "freq": 435_612_500,
                        "mode": "FM",
                        "rx": True,
                        "tx": False,
                    }
                ],
            }
        ).encode()
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sender:
                sender.sendto(payload, ("127.0.0.1", port))
            self.assertTrue(received_event.wait(1.0))
        finally:
            listener.stop()

        frequency = next(event for event in received if event.property == "frequency")
        self.assertEqual(frequency.value, 435_612_500)
        self.assertEqual(
            frequency.classification,
            RadioStateClassification.EXTERNAL_CHANGE,
        )
        self.assertFalse(listener.running)

    def test_json_snapshot_change_becomes_generic_frequency_event(self) -> None:
        listener = HamlibAsyncStateListener(lambda _event: None)
        listener.seed_observed(RadioStateProperty.FREQUENCY, 435_612_000, "MainA")
        payload = json.dumps(
            {
                "app": "Hamlib",
                "version": "4.7.2",
                "seq": 2,
                "rig": {"name": "test"},
                "vfos": [
                    {
                        "name": "MainA",
                        "freq": 435_613_000,
                        "mode": "FM",
                        "rx": True,
                        "tx": False,
                    }
                ],
            }
        ).encode()

        events, sequence = listener._parse_snapshot(payload)
        frequency = next(event for event in events if event.property == "frequency")

        self.assertEqual(sequence, 2)
        self.assertEqual(frequency.value, 435_613_000)
        self.assertEqual(frequency.role, "rx")
        self.assertEqual(frequency.vfo, "MainA")
        self.assertEqual(frequency.classification, RadioStateClassification.EXTERNAL_CHANGE)

    def test_initial_snapshot_is_refresh_not_verification(self) -> None:
        listener = HamlibAsyncStateListener(lambda _event: None)
        payload = json.dumps(
            {
                "app": "Hamlib",
                "seq": 1,
                "vfos": [{"name": "VFOA", "freq": 14_074_000, "mode": "USB"}],
            }
        ).encode()

        events, _sequence = listener._parse_snapshot(payload)

        self.assertTrue(events)
        self.assertTrue(
            all(event.classification == RadioStateClassification.STATE_REFRESH for event in events)
        )

    def test_snapshot_ptt_is_not_treated_as_verified_async_ptt(self) -> None:
        listener = HamlibAsyncStateListener(lambda _event: None)
        payload = json.dumps(
            {
                "app": "Hamlib",
                "seq": 1,
                "vfos": [
                    {
                        "name": "VFOA",
                        "freq": 14_074_000,
                        "mode": "USB",
                        "ptt": True,
                        "rx": False,
                        "tx": True,
                    }
                ],
            }
        ).encode()

        events, _sequence = listener._parse_snapshot(payload)

        self.assertTrue(events)
        self.assertNotIn(RadioStateProperty.PTT, {event.property for event in events})


class HamlibCapabilityTests(unittest.TestCase):
    def test_managed_rigctld_uses_numeric_async_config_value(self) -> None:
        command = _build_rigctld_command(
            model_id=3081,
            serial_port="/dev/ttyUSB0",
            baud=115200,
            tcp_port=4532,
            async_port=4533,
            vfo_mode=True,
            debug_logging=False,
        )

        config = command[command.index("-C") + 1]
        self.assertEqual(
            config,
            (
                "async=1,multicast_data_addr=127.0.0.1,"
                "multicast_data_port=4533,poll_interval=0"
            ),
        )
        self.assertNotIn("async=True", config)

    @patch("pi_sat_controller.backend.radio.hamlib_async.subprocess.run")
    def test_pre_46_hamlib_falls_back_before_backend_probe(self, run) -> None:
        run.return_value = SimpleNamespace(
            returncode=0,
            stdout="rigctld Hamlib 4.5.5\n",
            stderr="",
        )

        capability = probe_hamlib_async_capability(3081)

        self.assertEqual(capability.state, AsyncCapabilityState.UNSUPPORTED)
        self.assertFalse(capability.backend_supported)
        self.assertEqual(run.call_count, 1)

    def test_version_parser_accepts_current_release_format(self) -> None:
        self.assertEqual(parse_hamlib_version("rigctld Hamlib 4.7.2"), (4, 7, 2))

    @patch("pi_sat_controller.backend.radio.hamlib_async.subprocess.run")
    def test_backend_async_capability_available(self, run) -> None:
        run.side_effect = [
            SimpleNamespace(returncode=0, stdout="rigctld Hamlib 4.6.2\n", stderr=""),
            SimpleNamespace(returncode=0, stdout="Has async data support: Y\n", stderr=""),
        ]

        capability = probe_hamlib_async_capability(3081)

        self.assertEqual(capability.state, AsyncCapabilityState.AVAILABLE)
        self.assertTrue(capability.backend_supported)

    @patch("pi_sat_controller.backend.radio.hamlib_async.subprocess.run")
    def test_backend_without_async_support_falls_back(self, run) -> None:
        run.side_effect = [
            SimpleNamespace(returncode=0, stdout="rigctld Hamlib 4.7.2\n", stderr=""),
            SimpleNamespace(returncode=0, stdout="Has async data support: N\n", stderr=""),
        ]

        capability = probe_hamlib_async_capability(9999)

        self.assertEqual(capability.state, AsyncCapabilityState.UNSUPPORTED)
        self.assertFalse(capability.backend_supported)

    def test_polling_only_never_starts_async(self) -> None:
        client = LocalHamlibClient(3081, "/dev/null", 115200, state_updates="polling")

        status = client.async_status()

        self.assertEqual(status["preference"], "polling")
        self.assertEqual(status["state"], "unsupported")
        self.assertFalse(status["listener_running"])


class _RunningListener:
    running = True

    def status(self) -> dict[str, object]:
        return {
            "listener_running": True,
            "last_async_event_monotonic": None,
            "listener_error": None,
        }


class LocalHamlibEventClassifierTests(unittest.TestCase):
    def make_client(self) -> LocalHamlibClient:
        client = LocalHamlibClient(
            3081,
            "/dev/ttyUSB0",
            115200,
            role_label="rx",
            target_vfo="MainA",
        )
        client._async_listener = _RunningListener()
        return client

    def test_recent_command_is_classified_as_self_echo_and_verifies_frequency(self) -> None:
        client = self.make_client()
        client._recent_commands.record(
            RadioStateProperty.FREQUENCY,
            435_612_300,
            role="rx",
            vfo="MainA",
        )

        client._handle_async_event(frequency_event(435_612_300, timestamp=monotonic()))
        events = client.drain_radio_state_events("rx")

        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].classification, RadioStateClassification.SELF_ECHO)
        self.assertTrue(client.is_async_property_verified(RadioStateProperty.FREQUENCY))

    def test_unmatched_frequency_is_classified_as_external_change(self) -> None:
        client = self.make_client()

        client._handle_async_event(frequency_event(435_613_200, timestamp=monotonic()))
        events = client.drain_radio_state_events("rx")

        self.assertEqual(len(events), 1)
        self.assertEqual(
            events[0].classification,
            RadioStateClassification.EXTERNAL_CHANGE,
        )

    def test_shared_radio_ignores_named_unconfigured_vfo_alias(self) -> None:
        client = self.make_client()
        client.register_role_vfo("tx", "SubA")

        client._handle_async_event(
            frequency_event(
                145_965_995,
                role="rx",
                vfo="VFOA",
                timestamp=monotonic(),
            )
        )

        self.assertEqual(client.drain_radio_state_events("rx"), [])
        self.assertEqual(client.drain_radio_state_events("tx"), [])

    def test_shared_radio_routes_only_exact_configured_vfo(self) -> None:
        client = self.make_client()
        client.register_role_vfo("tx", "SubA")

        client._handle_async_event(
            frequency_event(435_633_400, role="rx", vfo="MainA", timestamp=monotonic())
        )
        client._handle_async_event(
            frequency_event(145_965_995, role="rx", vfo="SubA", timestamp=monotonic())
        )

        rx_events = client.drain_radio_state_events("rx")
        tx_events = client.drain_radio_state_events("tx")
        self.assertEqual([event.value for event in rx_events], [435_633_400])
        self.assertEqual([event.value for event in tx_events], [145_965_995])

    def test_shared_crossband_frequency_overrides_wrong_vfo_label(self) -> None:
        client = self.make_client()
        client.register_role_vfo("tx", "SubA")
        client._remember_role_frequency("rx", 435_633_400)
        client._remember_role_frequency("tx", 145_965_995)

        client._handle_async_event(
            frequency_event(
                435_635_500,
                role="tx",
                vfo="SubA",
                timestamp=monotonic(),
            )
        )
        client._handle_async_event(
            frequency_event(
                145_965_200,
                role="rx",
                vfo="MainA",
                timestamp=monotonic(),
            )
        )

        rx_events = client.drain_radio_state_events("rx")
        tx_events = client.drain_radio_state_events("tx")
        self.assertEqual([event.value for event in rx_events], [435_635_500])
        self.assertEqual([event.value for event in tx_events], [145_965_200])

    def test_shared_frequency_hint_rejects_unrelated_third_band(self) -> None:
        client = self.make_client()
        client.register_role_vfo("tx", "SubA")
        client._remember_role_frequency("rx", 435_633_400)
        client._remember_role_frequency("tx", 145_965_995)

        client._handle_async_event(
            frequency_event(
                1_296_100_000,
                role="rx",
                vfo="VFOA",
                timestamp=monotonic(),
            )
        )

        self.assertEqual(client.drain_radio_state_events("rx"), [])
        self.assertEqual(client.drain_radio_state_events("tx"), [])

    def test_expired_command_echoes_do_not_remove_verification(self) -> None:
        client = self.make_client()
        now = monotonic()
        client._verified_properties["rx"].add(RadioStateProperty.FREQUENCY)
        for value in (435_612_100, 435_612_200, 435_612_300):
            client._recent_commands.record(
                RadioStateProperty.FREQUENCY,
                value,
                role="rx",
                vfo="MainA",
                sent_at=now - 3.0,
            )

        client._expire_recent_commands()

        self.assertTrue(client.is_async_property_verified(RadioStateProperty.FREQUENCY))

    def test_repeated_reconciliation_mismatch_removes_verification(self) -> None:
        client = self.make_client()
        client._verified_properties["rx"].add(RadioStateProperty.FREQUENCY)

        client.mark_async_property_unverified(
            RadioStateProperty.FREQUENCY,
            reason=(
                "repeated_reconciliation_mismatch count=3 "
                "cached=435612100 polled=435612200"
            ),
            role="rx",
        )

        self.assertFalse(client.is_async_property_verified(RadioStateProperty.FREQUENCY))


class LocalHamlibConnectionFallbackTests(unittest.TestCase):
    def test_async_start_failure_restarts_rigctld_in_polling_mode(self) -> None:
        client = LocalHamlibClient(3081, "/dev/ttyUSB0", 115200)
        polling_client = SimpleNamespace()

        with patch.object(client, "_prepare_async_listener", return_value=4533), patch.object(
            client,
            "_start_rigctld",
            side_effect=[RuntimeError("async config rejected"), polling_client],
        ) as start:
            connected = client._ensure_client()

        self.assertIs(connected, polling_client)
        self.assertEqual(start.call_args_list, [call(4533), call(None)])
        self.assertFalse(client._async_capability.backend_supported)

    def test_transport_reconnect_reprepares_async_listener(self) -> None:
        client = LocalHamlibClient(3081, "/dev/ttyUSB0", 115200)
        client._client = SimpleNamespace(is_broken=True, close=lambda: None)
        replacement = SimpleNamespace()

        with patch.object(client, "_prepare_async_listener", return_value=4533) as prepare, patch.object(
            client,
            "_start_rigctld",
            return_value=replacement,
        ) as start:
            connected = client._ensure_client()

        self.assertIs(connected, replacement)
        prepare.assert_called_once_with()
        start.assert_called_once_with(4533)


if __name__ == "__main__":
    unittest.main()
