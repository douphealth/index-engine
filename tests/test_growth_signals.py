"""Search opportunities and AI-crawler access: the rules that decide what the owner is told to do."""
import sqlite3
import unittest
from unittest.mock import patch

from engine import aiaccess, opportunities as op, store


def db_with(rows, titles=()):
    """An in-memory engine database holding page_queries rows (src, url, query, clicks, impr, pos) for site.example."""
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.executescript(store.SCHEMA)
    for sql in store.MIGRATIONS:
        try:
            db.execute(sql)
        except sqlite3.OperationalError:
            pass
    for src, url, q, c, i, pos in rows:
        db.execute("INSERT INTO page_queries(src, url, site, query, clicks, impr, pos, win_end) VALUES(?,?,?,?,?,?,?,?)",
                   (src, url, "site.example", q, c, i, pos, "2026-10-31"))
        db.execute("INSERT OR IGNORE INTO urls(url, site) VALUES(?, 'site.example')", (url,))
    for url, title in titles:
        db.execute("INSERT OR IGNORE INTO urls(url, site) VALUES(?, 'site.example')", (url,))
        db.execute("INSERT OR REPLACE INTO audits(url, title) VALUES(?, ?)", (url, title))
    return db


# A realistic curve's worth of data: well-ranked pages get clicked, low-ranked ones barely
BASE = [("bing", f"https://site.example/p{k}/", f"topic {k}", c, 1000, pos)
        for k, (c, pos) in enumerate([(120, 1.0), (90, 2.0), (40, 3.0), (25, 4.0), (18, 6.0), (10, 8.0), (5, 12.0)])]
LIVE = lambda db: {r[0]: ("INDEXED", []) for r in db.execute("SELECT url FROM urls")}  # noqa: E731


class CurveTests(unittest.TestCase):
    def test_curve_never_rises_with_position_even_when_the_data_does(self):
        rows = [(10, 1000, 1), (50, 1000, 2), (5, 1000, 3), (30, 1000, 6)]
        curve = op.ctr_curve(rows)
        self.assertEqual(len(curve), op.MAX_POS)
        self.assertTrue(all(curve[i] >= curve[i + 1] for i in range(op.MAX_POS - 1)))

    def test_thin_site_data_is_pulled_toward_the_portfolio(self):
        prior = [0.2] + [0.05] * (op.MAX_POS - 1)
        thin = op.ctr_curve([(0, 5, 1)], prior=prior)               # 5 impressions at position 1, no click
        self.assertGreater(thin[0], 0.15)                           # one unlucky handful does not make position 1 worthless

    def test_binomial_tail(self):
        self.assertAlmostEqual(op.p_at_most(0, 10, 0.5), 0.5 ** 10, places=9)
        self.assertAlmostEqual(op.p_at_most(10, 10, 0.3), 1.0, places=9)
        self.assertLess(op.p_at_most(0, 500, 0.05), 1e-9)


class OpportunityTests(unittest.TestCase):
    def test_close_to_top_is_found_and_its_gain_is_estimated(self):
        rows = BASE + [("bing", "https://site.example/near/", "how to water ferns", 2, 400, 7.0)]
        db = db_with(rows)
        res = op.analyze(db, "site.example", LIVE(db), prior={})
        item = next(x for x in res["items"] if x["query"] == "how to water ferns")
        self.assertEqual(item["kind"], "close_to_top")
        self.assertGreater(item["gain"], 5)
        self.assertNotIn("first-hand", item["advice"])

    def test_low_ctr_needs_a_shortfall_bigger_than_chance(self):
        clear = ("bing", "https://site.example/snip/", "fern care guide", 0, 600, 2.0)      # 0 clicks where ~9% is usual
        noisy = ("bing", "https://site.example/noisy/", "fern soil", 3, 85, 2.0)          # a few short, could be luck
        db = db_with(BASE + [clear, noisy], titles=[("https://site.example/snip/", "Fern | Site")])
        res = op.analyze(db, "site.example", LIVE(db), prior={})
        kinds = {x["query"]: x["kind"] for x in res["items"]}
        self.assertEqual(kinds.get("fern care guide"), "low_ctr")
        self.assertNotIn("fern soil", kinds)
        snip = next(x for x in res["items"] if x["query"] == "fern care guide")
        self.assertEqual(snip["description"], "missing")

    def test_two_pages_sharing_a_search_is_a_split(self):
        rows = BASE + [("bing", "https://site.example/a/", "repot a fern", 1, 120, 8.0),
                       ("bing", "https://site.example/b/", "repot a fern", 0, 90, 9.5)]
        db = db_with(rows)
        res = op.analyze(db, "site.example", LIVE(db), prior={})
        split = [x for x in res["items"] if x["kind"] == "split"]
        self.assertEqual([x["query"] for x in split], ["repot a fern"])
        self.assertEqual({p["url"] for p in split[0]["pages"]}, {"https://site.example/a/", "https://site.example/b/"})

    def test_brand_number_and_blocked_searches_are_left_out(self):
        rows = BASE + [("bing", "https://site.example/near/", "sitebrand fern tips", 0, 400, 7.0),
                       ("bing", "https://site.example/near/", "17-8", 0, 400, 5.0),
                       ("bing", "https://site.example/gone/", "dead page search", 0, 400, 7.0)]
        titles = [(f"https://site.example/t{k}/", f"Post {k} | SiteBrand") for k in range(5)]
        db = db_with(rows, titles)
        buckets = LIVE(db)
        buckets["https://site.example/gone/"] = ("NOINDEX", [])
        res = op.analyze(db, "site.example", buckets, prior={})
        found = {x["query"] for x in res["items"]}
        self.assertFalse({"sitebrand fern tips", "17-8", "dead page search"} & found)
        self.assertIn("sitebrand", op.brands(db, "site.example"))

    def test_one_page_with_many_wordings_is_one_item_with_the_summed_gain(self):
        near = "https://site.example/near/"
        rows = BASE + [("bing", near, q, 0, 300, 6.0) for q in ("water ferns", "how to water ferns", "fern watering")]
        db = db_with(rows)
        res = op.analyze(db, "site.example", LIVE(db), prior={})
        mine = [x for x in res["items"] if x.get("url") == near]
        self.assertEqual(len(mine), 1)
        self.assertEqual(len(mine[0]["queries"]), 3)
        self.assertAlmostEqual(mine[0]["gain"], sum(q["gain"] for q in mine[0]["queries"]), places=0)
        close = [x for x in res["items"] if x["kind"] == "close_to_top"]
        self.assertEqual(len(close), len({x["url"] for x in close}))                      # never two items for one page
        self.assertEqual(res["totals"]["close_to_top"], len(close))
        self.assertEqual(res["totals"]["searches"], sum(len(x["queries"]) for x in res["items"] if x["kind"] != "split"))

    def test_summary_is_compact(self):
        db = db_with(BASE + [("bing", "https://site.example/near/", "how to water ferns", 2, 400, 7.0)])
        s = op.site_summary(op.analyze(db, "site.example", LIVE(db), prior={}))
        self.assertEqual(set(s) >= {"close_to_top", "low_ctr", "split", "gain", "top", "pairs"}, True)
        self.assertLessEqual(len(s["top"]), 3)


class AiAccessTests(unittest.TestCase):
    def test_states(self):
        self.assertEqual(aiaccess.state_of(False, None, False, True), "blocked_robots")
        self.assertEqual(aiaccess.state_of(True, 403, False, True), "blocked_edge")
        self.assertEqual(aiaccess.state_of(True, 200, True, True), "blocked_edge")       # a challenge page answered
        self.assertEqual(aiaccess.state_of(True, 403, False, False), "allowed")           # the site was down: do not blame the bot
        self.assertEqual(aiaccess.state_of(True, 200, False, True), "allowed")

    def test_robots_and_edge_are_read_and_a_change_raises_one_grouped_alert(self):
        class Resp:
            def __init__(self, code, text="", headers=None):
                self.status_code, self.text, self.content, self.headers = code, text, text.encode(), headers or {}

        robots = "User-agent: GPTBot\nDisallow: /\n\nUser-agent: *\nAllow: /\n"

        class Session:
            def __init__(self, edge_block=()):
                self.edge_block = edge_block

            def get(self, url, headers=None, timeout=None, allow_redirects=True):
                if url.endswith("/robots.txt"):
                    return Resp(200, robots)
                ua = (headers or {}).get("User-Agent", "")
                return Resp(403 if any(b in ua for b in self.edge_block) else 200, "<html>ok</html>")

        db = db_with([])
        with patch.object(aiaccess, "PAUSE_S", 0):
            aiaccess.run(db, ["site.example"], session=Session())
            first = aiaccess.summary(db, "site.example")
            self.assertEqual(first["search_blocked"], [])
            self.assertIn("GPTBot", first["training_blocked"])
            self.assertEqual(db.execute("SELECT COUNT(*) FROM alerts").fetchone()[0], 0)       # the first check is a baseline
            aiaccess.run(db, ["site.example"], session=Session(edge_block=("OAI-SearchBot", "PerplexityBot")))
        after = aiaccess.summary(db, "site.example")
        self.assertEqual(sorted(after["search_blocked"]), ["OAI-SearchBot", "PerplexityBot"])
        alerts = db.execute("SELECT kind, severity, message FROM alerts").fetchall()
        self.assertEqual([(a["kind"], a["severity"]) for a in alerts], [("ai_search_blocked", "high")])
        self.assertIn("OAI-SearchBot", alerts[0]["message"])
        self.assertIn("PerplexityBot", alerts[0]["message"])


if __name__ == "__main__":
    unittest.main()
