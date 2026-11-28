"""Measured results of applied links: difference in differences against unlinked pages, and calibration."""
import datetime as dt
import json
import sqlite3
import unittest
from unittest.mock import patch

from engine import links, results, store

SITE = "plants.test"
PROP = "sc-domain:plants.test"
T = "https://plants.test/target/"


def ago(days):
    return (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days)).replace(microsecond=0).isoformat()


class FakeGSC:
    """search_pages answers per window: before = the window ending before the link day, after = the other."""

    def __init__(self, link_day, before, after, queries=None):
        self.link_day, self.before, self.after, self.queries = link_day, before, after, queries or {}
        self.calls = 0

    def search_pages(self, prop, start, end):
        self.calls += 1
        data = self.before if end < self.link_day else self.after
        return [{"keys": [u], "clicks": c, "impressions": i, "position": p} for u, (c, i, p) in data.items()]

    def search_page_queries(self, prop, page, start, end):
        self.calls += 1
        return self.queries.get("before" if end < self.link_day else "after", {})


def controls(n, impr, pos, scale=1.0):
    return {f"https://plants.test/c{i}/": (int(10 * scale), int(impr * scale), pos) for i in range(n)}


class GrowthMathTest(unittest.TestCase):
    def test_target_beating_controls_shows_lift(self):
        before = {T: (10, 1000, 8.0), **controls(6, 1000, 8.0)}
        after = {T: (30, 2000, 5.0), **controls(6, 1000, 8.0)}
        out = results.measure_growth(T, "water philodendron", before, after, set())
        self.assertGreater(out["impr_lift"], 0.9)
        self.assertGreater(out["clicks_lift"], 1.5)
        self.assertEqual(out["pos_change"], -3.0)                    # negative = moved up

    def test_site_wide_swing_cancels(self):
        before = {T: (10, 1000, 8.0), **controls(6, 1000, 8.0)}
        after = {T: (15, 1500, 8.0), **{u: (15, 1500, 8.0) for u in controls(6, 1000, 8.0)}}
        out = results.measure_growth(T, None, before, after, set())
        self.assertAlmostEqual(out["impr_lift"], 0.0, places=2)

    def test_linked_pages_are_never_controls(self):
        before = {T: (10, 1000, 8.0), "https://plants.test/other-linked/": (10, 1000, 8.0)}
        self.assertIsNone(results.measure_growth(T, None, before, before, {"https://plants.test/other-linked/"}))


class MeasureTest(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        self.db.row_factory = sqlite3.Row
        self.db.executescript(store.SCHEMA)
        self.addCleanup(self.db.close)
        patcher = patch.object(results, "log")
        patcher.start()
        self.addCleanup(patcher.stop)

    def link(self, target, days, status="applied", anchor="water philodendron", n=1):
        self.db.execute("INSERT INTO link_ops(id, site, source, target, anchor, status, applied_at) VALUES(?,?,?,?,?,?,?)",
                        (f"{target}{n}", SITE, "https://plants.test/source/", target, anchor, status, ago(days)))

    def inspected(self, url, days, coverage):
        self.db.execute("INSERT INTO gsc_history VALUES(?,?,?)", (url, ago(days), coverage))
        self.db.execute("INSERT OR IGNORE INTO urls(url, site) VALUES(?,?)", (url, SITE))

    def test_growth_page_is_measured_once_with_its_search(self):
        self.inspected(T, 60, "Submitted and indexed")
        self.link(T, 40)
        link_day = dt.datetime.fromisoformat(ago(40)).date()
        client = FakeGSC(link_day, {T: (10, 1000, 9.0), **controls(6, 1000, 9.0)}, {T: (25, 1800, 6.0), **controls(6, 1000, 9.0)},
                         {"before": {"water philodendron": (0, 300, 9.4)}, "after": {"water philodendron": (6, 500, 5.1)}})
        self.assertEqual(results.measure(self.db, client, {SITE: PROP}), 1)
        row = self.db.execute("SELECT kind, data FROM results WHERE target=?", (T,)).fetchone()
        data = json.loads(row["data"])
        self.assertEqual(row["kind"], "growth")
        self.assertEqual(data["anchor_query"], {"query": "water philodendron", "pos_before": 9.4, "pos_after": 5.1,
                                                "impr_before": 300, "impr_after": 500})
        self.assertGreater(data["impr_lift"], 0.5)
        self.assertEqual(results.measure(self.db, client, {SITE: PROP}), 0)          # never measured twice

    def test_too_recent_links_wait(self):
        self.inspected(T, 60, "Submitted and indexed")
        self.link(T, 10)
        client = FakeGSC(dt.date.today(), {}, {})
        self.assertEqual(results.measure(self.db, client, {SITE: PROP}), 0)
        self.assertEqual(client.calls, 0)

    def test_index_page_against_unlinked_peers(self):
        self.inspected(T, 50, "Discovered - currently not indexed")
        self.inspected(T, 30, "Submitted and indexed")
        for i in range(4):
            peer = f"https://plants.test/peer{i}/"
            self.inspected(peer, 50, "Discovered - currently not indexed")
            self.inspected(peer, 30, "Submitted and indexed" if i == 0 else "Discovered - currently not indexed")
        self.link(T, 40)
        client = FakeGSC(dt.date.today(), {}, {})
        self.assertEqual(results.measure(self.db, client, {SITE: PROP}), 1)
        data = json.loads(self.db.execute("SELECT data FROM results").fetchone()["data"])
        self.assertEqual((data["indexed"], data["control_rate"], data["controls"]), (True, 0.25, 4))
        self.assertEqual(results.summary(self.db)["index"], {"n": 1, "indexed_rate": 1.0, "control_rate": 0.25, "lift": 0.75})

    def test_a_failing_google_call_measures_nothing_and_does_not_raise(self):
        self.inspected(T, 60, "Submitted and indexed")
        self.link(T, 40)

        class Down(FakeGSC):
            def search_pages(self, *a):
                raise RuntimeError("HTTP 500")
        self.assertEqual(results.measure(self.db, Down(dt.date.today(), {}, {}), {SITE: PROP}), 0)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM results").fetchone()[0], 0)


class CalibrationTest(unittest.TestCase):
    def summary(self, kind, n, value):
        if kind == "growth":
            return {"growth": {"n": n, "median_impr_lift": value}, "index": None}
        return {"growth": None, "index": {"n": n, "lift": value}}

    def test_small_samples_change_nothing(self):
        got = results.calibration(None, self.summary("growth", 14, 0.9), exp={})
        self.assertEqual((got["growth"], got["index"]), (1.0, 1.0))

    def test_measured_effects_move_the_ranking_both_ways(self):
        self.assertEqual(results.calibration(None, self.summary("growth", 20, 0.3), exp={})["growth"], 1.5)
        self.assertEqual(results.calibration(None, self.summary("growth", 20, -0.1), exp={})["growth"], 0.5)
        self.assertEqual(results.calibration(None, self.summary("index", 20, 0.2), exp={})["index"], 1.5)
        self.assertEqual(results.calibration(None, self.summary("index", 20, 0.0), exp={})["index"], 0.714)

    def test_sign_test(self):
        self.assertEqual(results._sign_p(10, 10), 0.002)
        self.assertEqual(results._sign_p(5, 10), 1.0)

    def test_growth_gain_follows_calibration(self):
        names = {T: ([["water", "philodendron", "tip"]], [])}
        opp = {"items": [{"kind": "close_to_top", "src": "google", "url": T, "gain": 50,
                          "queries": [{"query": "water philodendron", "pos": 7.0, "impr": 900}]}]}
        db = sqlite3.connect(":memory:")
        db.row_factory = sqlite3.Row
        db.executescript(store.SCHEMA)
        buckets = {T: ("INDEXED", set())}
        _k, base = links.growth_targets(db, SITE, opp, buckets, names, {}, set())
        _k, half = links.growth_targets(db, SITE, opp, buckets, names, {}, set(), scale=0.5)
        self.assertAlmostEqual(half[T], base[T] / 2)
        db.close()


if __name__ == "__main__":
    unittest.main()
