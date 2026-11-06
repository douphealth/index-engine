"""Growth anchors: an indexed page close to the top for a search gets links whose anchor is that search."""
import sqlite3
import unittest
from unittest.mock import patch

from engine import links, store

SITE = "plants.test"
TARGET = "https://plants.test/philodendron-watering-tips/"
SOURCE = "https://plants.test/monstera-care-guide/"
RIVAL = "https://plants.test/philodendron-care/"
QUERY = "how often to water philodendron"
SENTENCE = ("<p>Readers who keep both plants often ask how often to water philodendron vines compared with "
            "monstera, and the answer depends on light, pot size and the season.</p>")


class FakeWP:
    ok = True

    def __init__(self, texts):
        self.texts = texts

    def corpus(self, after=None):
        for i, (url, raw) in enumerate(self.texts.items()):
            yield {"url": url, "raw": raw, "modified": "2026-11-01T00:00:00", "id": i + 1, "type": "posts"}


def row(url, kind="post"):
    return {"url": url, "kind": kind, "gsc_clicks": 5, "bing_clicks": 2}


class GrowthLinkTest(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        self.db.row_factory = sqlite3.Row
        self.db.executescript(store.SCHEMA)
        for url, title in ((TARGET, "10 Essential Philodendron Watering Tips for Lush Growth"),
                           (SOURCE, "Monstera Care Guide for Beginners"),
                           (RIVAL, "Pothos and Trailing Vines Explained")):
            self.db.execute("INSERT INTO audits(url, title) VALUES(?, ?)", (url, title))
        self.query(TARGET, QUERY, 5.7)
        self.buckets = {TARGET: ("INDEXED", set()), SOURCE: ("INDEXED", set()), RIVAL: ("INDEXED", set())}
        self.opps = {"items": [{"kind": "close_to_top", "src": "google", "url": TARGET, "gain": 61.0,
                                "queries": [{"query": QUERY, "pos": 5.7, "impr": 1972, "clicks": 0, "gain": 61.0}]}]}
        self.texts = {SOURCE: SENTENCE, TARGET: "<p>Target body text without any link back to the source page here.</p>",
                      RIVAL: "<p>Pothos vines grow quickly in bright indirect light and forgive missed waterings.</p>"}
        patcher = patch.object(links, "log")
        patcher.start()
        self.addCleanup(patcher.stop)

    def query(self, url, q, pos):
        self.db.execute("INSERT INTO page_queries(src, url, site, query, clicks, impr, pos, win_end) VALUES('google',?,?,?,0,500,?,'2026-11-01')",
                        (url, SITE, q, pos))

    def propose(self):
        results = {SITE: {"buckets": self.buckets, "rows": [row(u) for u in self.buckets], "opportunities": self.opps}}
        return links.propose(self.db, results, {}, {SITE: FakeWP(self.texts)})

    def test_search_wording_becomes_the_anchor_with_its_reason(self):
        out = self.propose()
        self.assertEqual(len(out), 1)
        p = out[0]
        self.assertEqual((p["source"], p["target"]), (SOURCE, TARGET))
        self.assertEqual(p["anchor"].lower(), QUERY)
        self.assertIn("Ranks about #6 on Google", p["why"])
        self.assertGreater(p["gain"], 0)

    def test_no_anchor_when_another_page_ranks_better_for_the_search(self):
        self.query(RIVAL, QUERY, 3.0)
        self.assertEqual(self.propose(), [])

    def test_no_anchor_when_the_wording_is_another_pages_title(self):
        self.db.execute("UPDATE audits SET title='How Often to Water Philodendron Indoors' WHERE url=?", (RIVAL,))
        out = self.propose()
        self.assertTrue(all(p["anchor"].lower() != QUERY for p in out))

    def test_only_indexed_pages_get_search_anchors(self):
        self.buckets[TARGET] = ("CRAWLED_NOT_INDEXED", set())
        self.assertEqual([p for p in self.propose() if p["why"] and "Ranks about" in p["why"]], [])

    def test_source_that_already_links_is_skipped(self):
        self.texts[SOURCE] = SENTENCE + f'<p>See also <a href="{TARGET}">our tips</a> for more detail on this.</p>'
        self.assertEqual(self.propose(), [])

    def test_query_phrases(self):
        self.assertIn(("how", "often", "to", "water", "philodendron"), links.query_phrases(QUERY))
        self.assertIn(("water", "philodendron"), links.query_phrases(QUERY, {"water", "philodendron", "tip"}))
        self.assertNotIn(("often", "to", "water", "philodendron"), links.query_phrases(QUERY, {"water", "philodendron"}))
        self.assertEqual(links.query_phrases("philodendron"), [])
        self.assertEqual(links.query_phrases("best philodendron 2026"), [])        # a year is never part of an anchor


if __name__ == "__main__":
    unittest.main()
