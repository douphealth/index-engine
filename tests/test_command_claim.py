"""Dashboard commands must never execute without an accepted atomic claim."""
import unittest
from unittest.mock import Mock, patch

from indexengine import claim_command


class ClaimTests(unittest.TestCase):
    def test_claim_accepted(self):
        dash = Mock()
        dash.command.return_value = {"ok": True}
        self.assertTrue(claim_command(dash, 17))
        dash.command.assert_called_once_with(17, "running")

    def test_rejected_claim(self):
        dash = Mock()
        dash.command.return_value = {"ok": False}
        self.assertFalse(claim_command(dash, 17))

    def test_unavailable_claim(self):
        dash = Mock()
        dash.command.side_effect = RuntimeError("connection unavailable")
        with patch("indexengine.safe", return_value=None):
            self.assertFalse(claim_command(dash, 17))

    def test_no_connection(self):
        self.assertFalse(claim_command(None, 17))


if __name__ == "__main__":
    unittest.main()
