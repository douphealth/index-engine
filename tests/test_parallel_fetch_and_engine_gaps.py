"""Sites are read side by side and stored one at a time, and a page one engine ranks is a prospect on the other.

Both come from 26 Nov 2026. A run of 22 sites spent 4.6 of its 7.8 minutes asking Google, Bing and the sites one
question at a time, one site after another. And the query data held 24 pages Bing showed in its top 8 for a search
while Google had not indexed them or did not show them for it: pages already proven able to rank, listed nowhere."""
import contextlib
import io
import sqlite3
import threading
import unittest
from unittest.mock import patch

from engine import config, discover, opportunities as op, store, traffic, util

SITE = "site.example"


def memory_db():
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.executescript(store.SCHEMA)
    for migration in store.MIGRATIONS:
        try:
            db.execute(migration)
        except sqlite3.OperationalError:
            pass
    return db


class Fetched(unittest.TestCase):
    def test_results_come_back_in_the_items_order(self):
        release = {"a": threading.Event(), "b": threading.Event(), "c": threading.Event()}

        def fetch(item):
            release[item].wait(5)                      # "a" finishes last
            return item.upper()

        release["c"].set(), release["b"].set()
        threading.Timer(0.05, release["a"].set).start()
        self.assertEqual([(i, r, e) for i, r, e in util.fetched("abc", fetch, workers=3)],
                         [("a", "A", None), ("b", "B", None), ("c", "C", None)])

    def test_items_really_overlap(self):
        together = threading.Barrier(3, timeout=5)     # passes only when three fetches are in flight at once

        def fetch(item):
            together.wait()
            return item

        self.assertEqual([r for _, r, e in util.fetched(range(3), fetch, workers=3) if e is None], [0, 1, 2])

    def test_one_failure_is_handed_back_and_costs_the_others_nothing(self):
        def fetch(item):
            if item == 2:
                raise RuntimeError("throttled")
            return item * 10

        got = list(util.fetched([1, 2, 3], fetch, workers=3))
        self.assertEqual([(i, r) for i, r, _ in got], [(1, 10), (2, None), (3, 30)])
        self.assertIsInstance(got[1][2], RuntimeError)
        self.assertEqual([e for _, _, e in got if e is None], [None, None])

    def test_what_a_worker_logs_is_printed_when_its_result_is_taken(self):
        first_done = threading.Event()

        def fetch(item):
            if item == "a":
                first_done.wait(5)                     # "b" logs before "a" does
            util.log(f"  read {item}")
            if item == "b":
                first_done.set()
            return item

        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            for item, _, _ in util.fetched(["a", "b"], fetch, workers=2):
                util.log(f"[site] {item}")
        self.assertEqual(out.getvalue().split("\n")[:4], ["  read a", "[site] a", "  read b", "[site] b"])

    def test_one_worker_reads_one_at_a_time_and_nothing_stays_held(self):
        seen = []
        self.assertEqual([r for _, r, _ in util.fetched([1, 2], lambda x: seen.append(x) or x, workers=1)], [1, 2])
        self.assertEqual(seen, [1, 2])
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            util.log("after")                          # the calling thread's own lines are never held back
        self.assertEqual(out.getvalue().strip(), "after")

    def test_nothing_to_read(self):
        self.assertEqual(list(util.fetched([], lambda x: x)), [])


class FakeBing:
    def __init__(self, broken=()):
        self.broken, self.threads = set(broken), set()

    def rank_traffic(self, burl):
        self.threads.add(threading.get_ident())
        if burl in self.broken:
            raise RuntimeError("GetRankAndTrafficStats HTTP 500")
        return [{"Date": "/Date(1793491200000)/", "Clicks": 3, "Impressions": 40}]

    def crawl_stats(self, burl):
        return [{"Date": "/Date(1793491200000)/", "CrawledPages": 7, "InIndex": 5}]


class ManySitesAtOnce(unittest.TestCase):
    def test_traffic_of_every_site_is_stored_and_a_failing_site_keeps_the_rest(self):
        db = memory_db()                               # sqlite refuses another thread, so storing proves where it ran
        sites = {f"s{k}.example": f"https://s{k}.example/" for k in range(6)}
        bing = FakeBing(broken={"https://s2.example/"})
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            traffic.pull(db, None, {}, bing, sites)
        stored = {r["site"] for r in db.execute("SELECT DISTINCT site FROM traffic_daily WHERE src='bing'")}
        self.assertEqual(stored, set(sites) - {"s2.example"})
        self.assertIn("[traffic] s2.example: Bing statistics failed", out.getvalue())
        self.assertNotIn(threading.get_ident(), bing.threads)          # the reading was done off the storing thread
        lines = [x.split(":")[0] for x in out.getvalue().splitlines() if x.startswith("[traffic]")]
        self.assertEqual(lines, [f"[traffic] {s}" for s in sites])     # the log keeps the sites' order

    def test_discovery_stores_sites_in_order_and_keeps_a_site_whose_reading_failed(self):
        db = memory_db()
        db.execute("INSERT INTO urls(url, site, in_sitemap) VALUES('https://b.example/old/', 'b.example', 1)")
        store.set_meta(db, "b.example", sitemap_count=1)

        def read(site, use_wp=True, was_wp=False):
            if site == "b.example":
                raise OSError("connection reset")
            url = f"https://{site}/page/"
            return {"base": f"https://{site}", "robots": "", "roots": [f"https://{site}/sitemap.xml"],
                    "found": {url: ("page", f"https://{site}/sitemap.xml", None)}, "read": [f"https://{site}/sitemap.xml"],
                    "errors": [], "wp": None, "wp_complete": False, "feed": None}

        out = io.StringIO()
        with patch.object(discover, "read_site", side_effect=read), contextlib.redirect_stdout(out):
            discover.run(db, ["a.example", "b.example", "c.example"], use_wp=False)
        self.assertEqual({r["url"] for r in db.execute("SELECT url FROM urls WHERE in_sitemap=1")},
                         {"https://a.example/page/", "https://b.example/old/", "https://c.example/page/"})
        self.assertEqual(store.get_meta(db, "b.example").get("sitemap_count"), 1)
        heads = [x for x in out.getvalue().splitlines() if x.startswith("[discover]")]
        self.assertEqual(heads, ["[discover] a.example", "[discover] b.example", "[discover] c.example"])
        self.assertIn("discovery failed (OSError", out.getvalue())

    def test_the_stages_ask_bing_more_gently_than_the_sites(self):
        self.assertLessEqual(config.BING_WORKERS, config.FETCH_WORKERS)
        self.assertLessEqual(config.FETCH_WORKERS, 8)


def gap_db(rows, titles, google=True):
    """page_queries rows (src, url, query, clicks, impr, pos) for one site whose pages have the given titles."""
    db = memory_db()
    for src, url, q, c, i, pos in rows:
        db.execute("INSERT INTO page_queries(src, url, site, query, clicks, impr, pos, win_end) VALUES(?,?,?,?,?,?,?,?)",
                   (src, url, SITE, q, c, i, pos, "2026-11-20"))
    for url, title in titles.items():
        db.execute("INSERT OR IGNORE INTO urls(url, site) VALUES(?, ?)", (url, SITE))
        db.execute("INSERT OR REPLACE INTO audits(url, title) VALUES(?, ?)", (url, title))
    if google:
        store.set_meta(db, SITE, gsc_property="sc-domain:" + SITE)
    return db


U = "https://site.example/mouse-droppings/"
T = {U: "Mouse Droppings: What They Look Like"}


def gaps(db, buckets):
    res = op.analyze(db, SITE, buckets, prior={})
    return res, [x for x in res["items"] if x["kind"] == "engine_gap"]


class EngineGap(unittest.TestCase):
    def test_bing_ranks_a_page_google_never_indexed(self):
        db = gap_db([("bing", U, "mouse droppings", 4, 272, 3.0), ("bing", U, "mouse poop", 1, 60, 5.0)], T)
        res, found = gaps(db, {U: ("UNKNOWN_TO_GOOGLE", [])})
        self.assertEqual(len(found), 1)                               # one item per page, not per wording
        item = found[0]
        self.assertEqual((item["google"], item["query"], item["impr_total"], item["gain"]), ("not_indexed", "mouse droppings", 332, None))
        self.assertEqual([q["query"] for q in item["queries"]], ["mouse droppings", "mouse poop"])
        self.assertIn("request indexing in Search Console", item["advice"])
        self.assertIn("1 more search", item["advice"])
        self.assertEqual((res["totals"]["engine_gap"], res["totals"]["engine_gap_unindexed"]), (1, 1))

    def test_a_page_google_crawled_and_declined_is_not_told_a_request_is_enough(self):
        db = gap_db([("bing", U, "mouse droppings", 4, 272, 3.0)], T)
        _, found = gaps(db, {U: ("CRAWLED_NOT_INDEXED", [])})
        self.assertIn("A request alone rarely changes that verdict", found[0]["advice"])
        self.assertIn("crawled the page and did not index it", found[0]["advice"])

    def test_indexed_page_google_shows_far_lower_or_not_at_all(self):
        far = gap_db([("bing", U, "mouse droppings", 4, 272, 3.0), ("google", U, "mouse droppings", 0, 6, 49.0)], T)
        _, found = gaps(far, {U: ("INDEXED", [])})
        self.assertEqual((found[0]["google"], found[0]["google_pos"]), ("indexed", 49.0))
        self.assertIn("reports it at about position 49", found[0]["advice"])
        absent = gap_db([("bing", U, "mouse droppings", 4, 272, 3.0), ("google", U, "mice", 0, 90, 20.0)], T)
        res, found = gaps(absent, {U: ("INDEXED", [])})
        self.assertIsNone(found[0]["google_pos"])
        self.assertIn("reports no impressions for it", found[0]["advice"])
        self.assertEqual(res["totals"]["engine_gap_unindexed"], 0)

    def test_no_gap_where_google_ranks_the_page_too(self):
        db = gap_db([("bing", U, "mouse droppings", 4, 272, 3.0), ("google", U, "mouse droppings", 9, 300, 6.0)], T)
        self.assertEqual(gaps(db, {U: ("INDEXED", [])})[1], [])

    def test_only_strong_bing_evidence_counts(self):
        weak = [("bing", U, "mouse droppings", 0, 272, 11.0),         # not in Bing's top 8
                ("bing", U, "mouse droppings size", 0, 12, 2.0)]      # too few impressions
        self.assertEqual(gaps(gap_db(weak, T), {U: ("UNKNOWN_TO_GOOGLE", [])})[1], [])

    def test_searches_the_page_is_not_about_are_left_out(self):
        rows = [("bing", U, "read more about construction software for small building firms", 0, 77, 5.5),   # a sentence
                ("bing", U, "garden hose reel", 0, 80, 4.0),          # no word of it in the title, heading or address
                ("bing", U, "17-8", 0, 297, 6.5)]                     # arithmetic, not a topic
        self.assertEqual(gaps(gap_db(rows, T), {U: ("UNKNOWN_TO_GOOGLE", [])})[1], [])

    def test_no_claim_without_google_data_or_a_known_verdict(self):
        rows = [("bing", U, "mouse droppings", 4, 272, 3.0)]
        self.assertEqual(gaps(gap_db(rows, T, google=False), {U: ("UNKNOWN_TO_GOOGLE", [])})[1], [])   # no property
        self.assertEqual(gaps(gap_db(rows, T), {U: ("NOT_INSPECTED", [])})[1], [])                     # verdict unknown
        self.assertEqual(gaps(gap_db(rows, T), {U: ("NOINDEX", [])})[1], [])                           # blocked on purpose
        self.assertEqual(gaps(gap_db(rows, T), {U: ("UNKNOWN_TO_GOOGLE", ["IGNORED"])})[1], [])        # owner said ignore

    def test_gaps_follow_the_estimated_items_and_fit_the_summary(self):
        near = "https://site.example/near/"
        curve = [("bing", f"https://site.example/p{k}/", f"topic {k}", c, 1000, pos)
                 for k, (c, pos) in enumerate([(300, 1), (150, 2), (100, 3), (60, 5), (30, 8), (10, 12)])]
        rows = curve + [("bing", near, "how to water ferns", 2, 400, 7.0), ("bing", U, "mouse droppings", 4, 272, 3.0)]
        titles = {**T, near: "How to Water Ferns", **{r[1]: f"Topic {k}" for k, r in enumerate(curve)}}
        buckets = {u: ("INDEXED", []) for u in titles}
        buckets[U] = ("DISCOVERED_NOT_INDEXED", [])
        res, found = gaps(gap_db(rows, titles), buckets)
        kinds = [x["kind"] for x in res["items"]]
        self.assertEqual(kinds[-1], "engine_gap")
        self.assertIn("close_to_top", kinds[:-1])
        self.assertEqual(res["totals"]["searches"], sum(len(x["queries"]) for x in res["items"] if x["kind"] != "split"))
        summary = op.site_summary(res)
        # The well-ranked Bing pages of the curve are gaps too: Google has them indexed and reports nothing for them.
        self.assertEqual((summary["engine_gap"], summary["engine_gap_unindexed"]), (len(found), 1))
        self.assertEqual([x["url"] for x in found if x["google"] == "not_indexed"], [U])
        self.assertTrue(all(x["kind"] != "engine_gap" or x["gain"] is None for x in res["items"]))


class FakeWP:
    ok = True

    def __init__(self, texts, fail=False):
        self.texts, self.fail, self.thread = texts, fail, None

    def corpus(self, after=None):
        self.thread = threading.get_ident()
        if self.fail:
            raise RuntimeError("GET posts page 1 -> HTTP 503")
        for i, (url, raw) in enumerate(self.texts.items()):
            yield {"url": url, "raw": raw, "modified": "2026-11-01T00:00:00", "id": i + 1, "type": "posts"}


class EngineGapLinks(unittest.TestCase):
    """A page Bing ranks for a search that Google does not show it for gets links worded as that search."""
    TARGET = "https://mice.test/foods-that-attract-mice/"
    SOURCE = "https://mice.test/prevent-mice-in-kitchen/"
    TEXT = ("<p>Crumbs, pet food and open bins are what attract mice most reliably, so a kitchen that is wiped "
            "down every evening gives them far less reason to stay.</p>")

    def setUp(self):
        self.db = memory_db()
        for url, title in ((self.TARGET, "12 Foods That Bring Rodents Indoors and What Attracts Mice"),
                           (self.SOURCE, "How to Prevent Mice in the Kitchen")):
            self.db.execute("INSERT INTO audits(url, title) VALUES(?, ?)", (url, title))
        self.db.execute("INSERT INTO page_queries(src, url, site, query, clicks, impr, pos, win_end) "
                        "VALUES('bing',?,?,'what attracts mice',1,64,5.3,'2026-11-20')", (self.TARGET, "mice.test"))
        self.buckets = {self.TARGET: ("INDEXED", set()), self.SOURCE: ("INDEXED", set())}
        self.texts = {self.SOURCE: self.TEXT, self.TARGET: "<p>The page itself, with no link to the kitchen guide in it.</p>"}
        patcher = patch.object(__import__("engine.links", fromlist=["log"]), "log")
        patcher.start()
        self.addCleanup(patcher.stop)

    def propose(self, kind, wp=None, more=None):
        from engine import links
        opps = {"items": [{"kind": kind, "src": "bing", "url": self.TARGET, "gain": None, "impr_total": 64, "google": "indexed",
                           "queries": [{"query": "what attracts mice", "pos": 5.3, "impr": 64, "clicks": 1, "gain": None}]}]}
        rows = [{"url": u, "kind": "post", "gsc_clicks": 5, "bing_clicks": 2} for u in self.buckets]
        results = {"mice.test": {"buckets": self.buckets, "rows": rows, "opportunities": opps}}
        self.wp = wp or FakeWP(self.texts)
        clients = {"mice.test": self.wp}
        for site, client in (more or {}).items():                    # further sites, each with one indexed page
            results[site] = {"buckets": {f"https://{site}/a/": ("INDEXED", set())}, "rows": [], "opportunities": {"items": []}}
            clients[site] = client
        return links.propose(self.db, results, {}, clients)

    def test_the_search_bing_ranks_it_for_becomes_the_anchor(self):
        out = self.propose("engine_gap")
        self.assertEqual([(p["source"], p["target"]) for p in out], [(self.SOURCE, self.TARGET)])
        self.assertIn("attract mice", out[0]["anchor"].lower())       # the search's wording, as the linking page says it
        self.assertIn("Bing shows it at about #5", out[0]["why"])
        self.assertIn("Google does not", out[0]["why"])
        self.assertGreater(out[0]["gain"], 0)                         # ranked by its Bing impressions, not left at zero

    def test_other_kinds_of_item_are_still_not_link_targets(self):
        self.assertEqual(self.propose("low_ctr"), [])

    def test_posts_are_read_off_the_storing_thread_and_a_failed_read_uses_the_stored_copy(self):
        other = FakeWP({"https://b.test/a/": "<p>A page of another site, read at the same time as the first one.</p>"})
        self.assertEqual(len(self.propose("engine_gap", more={"b.test": other})), 1)
        for client in (self.wp, other):
            self.assertNotIn(client.thread, (None, threading.get_ident()))
        again = self.propose("engine_gap", wp=FakeWP(self.texts, fail=True))      # WordPress is down on the next run
        self.assertEqual(len(again), 1)                                           # the stored posts still give the link


if __name__ == "__main__":
    unittest.main()
