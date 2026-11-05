"""Search Console links: use the report link the URL Inspection API returns; never put a page URL in ?id= (404)."""
import unittest
from unittest.mock import MagicMock

from engine import google, report

PROP = "sc-domain:example.test"
URL = "https://example.test/a-post/"
API_LINK = "https://search.google.com/search-console/inspect?resource_id=sc-domain:example.test&id=odaUL5Dqq3q8n0EicQzawg"


class InspectLinkTest(unittest.TestCase):
    def test_prefers_the_api_report_link(self):
        self.assertEqual(report.inspect_link(PROP, URL, API_LINK), API_LINK)

    def test_falls_back_to_the_property_home_without_the_url(self):
        link = report.inspect_link(PROP, URL)
        self.assertEqual(link, "https://search.google.com/search-console?resource_id=sc-domain%3Aexample.test")
        self.assertNotIn("example.test%2Fa-post", link)

    def test_rejects_foreign_links_and_missing_property(self):
        self.assertTrue(report.inspect_link(PROP, URL, "https://evil.test/x").startswith("https://search.google.com/"))
        self.assertEqual(report.inspect_link(None, URL), "")


class ClientParsesReportLinkTest(unittest.TestCase):
    def inspect(self, payload):
        client = google.GSCClient({})
        response = MagicMock(status_code=200, text="")
        response.json.return_value = payload
        client._s = lambda: MagicMock(post=MagicMock(return_value=response))
        return client.inspect(PROP, URL)

    def test_link_is_kept(self):
        res = self.inspect({"inspectionResult": {"inspectionResultLink": API_LINK,
                                                 "indexStatusResult": {"verdict": "PASS", "coverageState": "Submitted and indexed"}}})
        self.assertEqual((res["link"], res["verdict"]), (API_LINK, "PASS"))

    def test_missing_or_foreign_link_is_dropped(self):
        self.assertIsNone(self.inspect({"inspectionResult": {"indexStatusResult": {}}})["link"])
        self.assertIsNone(self.inspect({"inspectionResult": {"inspectionResultLink": "javascript:alert(1)"}})["link"])


if __name__ == "__main__":
    unittest.main()
