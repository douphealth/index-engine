"""Link gaps: unindexed pages no trusted page links to, with pages on the same subject that could link to them."""
import sqlite3
import unittest
from unittest.mock import patch

from engine import linkgaps, store

SITE = "example.test"
B = "https://" + SITE


class LinkGaps(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        self.db.row_factory = sqlite3.Row
        self.db.executescript(store.SCHEMA)
        for migration in store.MIGRATIONS:
            self.db.execute(migration)
        self.addCleanup(self.db.close)
        self.buckets = {}

    def page(self, slug, title, bucket, flags="", value=1.0, kind="post"):
        url = f"{B}/{slug}/"
        self.db.execute("INSERT INTO urls(url, site, kind, in_sitemap) VALUES (?,?,?,1)", (url, SITE, kind))
        self.db.execute("INSERT INTO audits(url, status, title) VALUES (?,200,?)", (url, title))
        self.db.execute("INSERT INTO triage(url, site, bucket, flags, value) VALUES (?,?,?,?,?)", (url, SITE, bucket, flags, value))
        self.buckets[url] = (bucket, flags)
        return url

    def filler(self, n):
        """Unrelated pages that are neither trusted nor wanted: they only make the site its real size."""
        for i in range(n):
            self.page(f"filler-{i}", f"Unrelated{i} Matter{i}", "NOINDEX")

    def link(self, src, dst, in_content=1):
        self.db.execute("INSERT INTO links(src, dst, in_content) VALUES (?,?,?)", (src, dst, in_content))

    def gaps(self):
        return linkgaps.analyze(self.db, SITE, self.buckets)

    def test_finds_unindexed_pages_without_a_link_from_a_trusted_page_and_suggests_related_ones(self):
        self.filler(15)
        hub = self.page("garmin-watches", "Garmin Watches Compared", "INDEXED")
        fenix = self.page("garmin-fenix-8-review", "Garmin Fenix 8 Review", "INDEXED")
        yoga = self.page("yoga-for-runners", "Yoga for Runners", "INDEXED")
        instinct = self.page("garmin-instinct-3-review", "Garmin Instinct 3 Review", "CRAWLED_NOT_INDEXED", value=5)
        linked = self.page("garmin-venu-review", "Garmin Venu Review", "UNKNOWN_TO_GOOGLE", value=9)
        self.link(hub, linked)                                   # already linked from an indexed page's text: not a gap
        self.link(yoga, instinct, in_content=0)                  # a menu or footer link does not count
        g = self.gaps()
        self.assertEqual((g["pages"], g["orphans"], g["trusted"]), (1, 0, 3))
        item, = g["items"]
        self.assertEqual((item["url"], item["inlinks"], item["bucket"]), (instinct, 1, "CRAWLED_NOT_INDEXED"))
        self.assertEqual({s["url"] for s in item["sources"]}, {hub, fenix})      # same subject; the yoga page is not offered
        self.assertTrue(all("garmin" in s["shared"] for s in item["sources"]))
        self.assertEqual(linkgaps.site_summary(g), {"pages": 1, "orphans": 0, "trusted": 3, "with_sources": 1})

    def test_orphans_are_counted_and_bing_earners_count_as_trusted(self):
        earner = self.page("mouse-traps", "Best Mouse Traps", "CRAWLED_NOT_INDEXED", flags="BING_EARNER")
        orphan = self.page("mouse-trap-bait", "Mouse Trap Bait Guide", "UNKNOWN_TO_GOOGLE")
        g = self.gaps()
        # The earner is itself unindexed and unlinked, and it is also the only trusted page.
        self.assertEqual((g["pages"], g["orphans"], g["trusted"]), (2, 2, 1))
        by_url = {x["url"]: x for x in g["items"]}
        self.assertEqual([s["url"] for s in by_url[orphan]["sources"]], [earner])
        self.assertEqual(by_url[earner]["sources"], [])          # a page is never its own source

    def test_blocked_ignored_taxonomy_and_utility_pages_are_not_listed(self):
        self.page("a-guide", "A Guide", "INDEXED")
        self.page("noindexed", "Noindexed Thing", "NOINDEX")
        self.page("ignored", "Ignored Thing", "UNKNOWN_TO_GOOGLE", flags="IGNORED")
        self.page("category-shoes", "Shoes", "UNKNOWN_TO_GOOGLE", kind="taxonomy")
        self.page("privacy-policy", "Privacy Policy", "UNKNOWN_TO_GOOGLE")
        self.assertEqual(self.gaps()["pages"], 0)

    def test_no_trusted_page_means_no_list(self):
        self.page("alone", "Alone", "UNKNOWN_TO_GOOGLE")
        self.assertIsNone(self.gaps())
        self.assertIsNone(linkgaps.site_summary(None))

    def test_one_page_is_not_suggested_for_everything_and_a_page_already_linking_is_not_suggested(self):
        self.filler(30)
        hub = self.page("power-bank-rules", "Power Bank Rules by Airline", "INDEXED")
        other = self.page("power-bank-limits", "Power Bank Limits Explained", "INDEXED")
        targets = [self.page(f"airline-{i}-power-bank-rules", f"Airline {i} Power Bank Rules", "UNKNOWN_TO_GOOGLE", value=10 - i)
                   for i in range(6)]
        self.link(other, targets[0], in_content=0)               # links from its footer: not offered as "could link"
        g = self.gaps()
        used = [s["url"] for x in g["items"] for s in x["sources"]]
        self.assertEqual(used.count(hub), linkgaps.MAX_PER_SOURCE)
        first = next(x for x in g["items"] if x["url"] == targets[0])
        self.assertNotIn(other, [s["url"] for s in first["sources"]])

    def test_large_site_needs_two_shared_words_and_ignores_words_on_many_pages(self):
        src = self.page("city-waste-treatment", "City Waste Treatment Contract", "INDEXED")
        one = self.page("city-audit-services", "City Audit Services", "UNKNOWN_TO_GOOGLE")
        two = self.page("waste-treatment-plant", "Waste Treatment Plant Works", "UNKNOWN_TO_GOOGLE")
        with patch.object(linkgaps, "LARGE_SITE", 3):
            by_url = {x["url"]: x for x in self.gaps()["items"]}
        self.assertEqual(by_url[one]["sources"], [])             # shares only "city"
        self.assertEqual([s["url"] for s in by_url[two]["sources"]], [src])
        with patch.object(linkgaps, "SUBJECT_MAX_PAGES", 1):     # "waste" and "treatment" are now on too many pages
            by_url = {x["url"]: x for x in self.gaps()["items"]}
        self.assertEqual(by_url[two]["sources"], [])


if __name__ == "__main__":
    unittest.main()
