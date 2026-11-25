"""Websites added from the dashboard: cleaned, deduplicated (with or without www.) and appended to config.SITES."""
import unittest
from unittest.mock import patch

from engine import config, dashboard


class SitesExtraTests(unittest.TestCase):
    def setUp(self):
        p = patch.object(config, "SITES", ["gearuptofit.com", "imagealchemy.app"])
        p.start()
        self.addCleanup(p.stop)

    def test_clean_domain(self):
        self.assertEqual(config.clean_domain(" https://Tender-Atlas.com/some/page?x=1 "), "tender-atlas.com")
        self.assertEqual(config.clean_domain("shop.example.co.uk:443"), "shop.example.co.uk")
        for bad in ("", "localhost", "javascript:alert(1)", "two words.com", "-bad.com", "a.b", "x" * 90 + ".com", "192.168.1.1"):
            self.assertIsNone(config.clean_domain(bad), bad)

    def test_added_sites_join_the_list_once(self):
        added = dashboard.register_sites({"sites_extra": ["tenderatlas.com", "www.gearuptofit.com", "TenderAtlas.com", "nope"]})
        self.assertEqual(added, ["tenderatlas.com"])
        self.assertEqual(config.SITES, ["gearuptofit.com", "imagealchemy.app", "tenderatlas.com"])
        self.assertEqual(dashboard.register_sites({"sites_extra": ["tenderatlas.com"]}), [])
        self.assertEqual(dashboard.register_sites({}), [])


if __name__ == "__main__":
    unittest.main()
