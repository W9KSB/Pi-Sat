from __future__ import annotations

import unittest
from unittest.mock import Mock

from pi_sat_controller.backend.radio.rigctld_client import (
    PersistentRigctldClient,
    RigctldClient,
)


class RigctldVfoModeProbeTests(unittest.TestCase):
    def test_transient_client_uses_hamlib_extended_command_syntax(self) -> None:
        client = RigctldClient("127.0.0.1", 4532)
        client._request = Mock(return_value="CHKVFO 1")

        self.assertTrue(client.check_vfo_mode())
        client._request.assert_called_once_with(r"\chk_vfo")

    def test_persistent_client_uses_hamlib_extended_command_syntax(self) -> None:
        client = PersistentRigctldClient("127.0.0.1", 4532)
        client._request = Mock(return_value="CHKVFO 1")

        self.assertTrue(client.check_vfo_mode())
        client._request.assert_called_once_with(r"\chk_vfo")

    def test_probe_rejects_disabled_vfo_mode(self) -> None:
        client = PersistentRigctldClient("127.0.0.1", 4532)
        client._request = Mock(return_value="CHKVFO 0")

        self.assertFalse(client.check_vfo_mode())


if __name__ == "__main__":
    unittest.main()
