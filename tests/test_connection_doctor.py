"""Read-only diagnostics should be useful even with absent credentials."""
import unittest
from unittest.mock import patch

from engine import doctor


class DoctorTests(unittest.TestCase):
    def test_offline_mode_never_calls_external_auth(self):
        with patch("engine.doctor.load_credentials", return_value=(None, [])), \
             patch("engine.doctor.config.CRED_FILE") as credential_path, \
             patch("engine.doctor.config.CF_TOKEN_FILE") as cf_token_path, \
             patch("engine.doctor.google.GSCClient") as gsc_client, \
             patch("engine.doctor.bing.BingClient") as bing_client:
            credential_path.is_file.return_value = False
            cf_token_path.is_file.return_value = False
            result = doctor.collect(online=False, check_indexnow=False)
            self.assertEqual(result["sources"]["google"]["status"], "missing")
            self.assertEqual(result["sources"]["bing"]["status"], "missing")
            self.assertEqual(result["sources"]["credential_file"]["status"], "missing")
            self.assertEqual(len(result["sites"]), 10)
            gsc_client.assert_not_called()
            bing_client.assert_not_called()
            self.assertFalse(any("private_key" in str(item) for item in result["sites"]))

    def test_property_matching_requires_verified_permission(self):
        self.assertIsNone(doctor.matched_property("example.com", [
            {"siteUrl": "sc-domain:example.com", "permissionLevel": "siteUnverifiedUser"}]))
        self.assertEqual(doctor.matched_property("example.com", [
            {"siteUrl": "sc-domain:example.com", "permissionLevel": "siteFullUser"}]),
            "sc-domain:example.com")

    def test_bing_matching_never_falls_back_to_substring_domains(self):
        self.assertIsNone(doctor.matched_bing("example.com", [{"Url": "https://notexample.com/"}]))
        self.assertEqual(doctor.matched_bing("example.com", [
            {"Url": "https://www.example.com/", "IsVerified": True}]), "https://www.example.com/")


if __name__ == "__main__":
    unittest.main()
