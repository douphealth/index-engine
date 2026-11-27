"""Alerts that matter stand out, and the request-indexing list is one the owner can finish.

On 21-27 Nov 2026, 166 of 171 warnings were pages that earned nothing being set to noindex on purpose, one alert each,
and the five real "dropped from Google" alerts were lost among them. The request-indexing list offered 10 pages per site
(up to 220 a day over 22 sites); 1,319 pages were listed from 23 Oct to 27 Nov and none was marked done."""
import json
import sqlite3
import unittest
from unittest.mock import patch

from engine import alerts, audit, config, store, triage, util

SITE = "example.test"
BASE = "https://example.test"


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


def indexable_page(db, path, site=SITE, base=BASE, words=800, clicks=0, coverage=None):
    url = f"{base}{path}"
    db.execute("INSERT INTO urls(url, site, kind, in_sitemap) VALUES(?,?,'post',1)", (url, site))
    db.execute("INSERT INTO audits(url, checked_at, status, hops, noindex, robots_google, robots_bing, canonical_state, words) "
               "VALUES(?,?,200,0,0,1,1,'self',?)", (url, util.now_iso(), words))
    if clicks:
        db.execute("INSERT INTO perf(url, site, bing_clicks) VALUES(?,?,?)", (url, site, clicks))
    if coverage:
        db.execute("INSERT INTO gsc(url, coverage) VALUES(?,?)", (url, coverage))
    return url


def fetched(url, noindex=0, status=200, words=800, hops=0, final=None):
    return {"url": url, "status": status, "hops": hops, "noindex": noindex, "noindex_src": "meta" if noindex else None,
            "canonical_state": "self", "canonical": None, "robots_google": 1, "words": words, "final_url": final or url}


def group(db, kind="pages_blocked", site=SITE):
    return db.execute("SELECT * FROM alerts WHERE site=? AND kind=? ORDER BY id", (site, kind)).fetchall()


class GroupedAlerts(unittest.TestCase):
    def setUp(self):
        self.db = memory_db()

    def test_pages_that_earn_nothing_make_one_alert_per_site(self):
        urls = [indexable_page(self.db, f"/p{i}/") for i in range(60)]
        for i in range(30):
            indexable_page(self.db, f"/keep{i}/")             # still indexable: the batch is a minority of the site
        for u in urls[:12]:
            audit.regressions(self.db, fetched(u, noindex=1))
        rows = group(self.db)
        self.assertEqual(len(rows), 1)
        self.assertIsNone(rows[0]["url"])
        self.assertEqual(rows[0]["severity"], "warn")
        self.assertTrue(rows[0]["message"].startswith("12 pages that earned no clicks and were not in Google's index "
                                                      "stopped being indexable (12 noindex (meta)): /p0/, /p1/, /p2/ and 9 more."),
                        rows[0]["message"])
        self.assertEqual(json.loads(rows[0]["data"])["n"], 12)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM alerts WHERE kind='became_blocked'").fetchone()[0], 0)

    def test_a_page_with_clicks_or_in_googles_index_keeps_its_own_high_alert(self):
        earner = indexable_page(self.db, "/earns/", clicks=4)
        indexed = indexable_page(self.db, "/indexed/", coverage="Submitted and indexed")
        crawled = indexable_page(self.db, "/crawled/", coverage="Crawled - currently not indexed")
        for u in (earner, indexed, crawled):
            audit.regressions(self.db, fetched(u, noindex=1))
        own = {r["url"]: r["severity"] for r in self.db.execute("SELECT url, severity FROM alerts WHERE kind='became_blocked'")}
        self.assertEqual(own, {earner: "high", indexed: "high"})
        self.assertEqual(json.loads(group(self.db)[0]["data"])["n"], 1, "a crawled-not-indexed page earns nothing: grouped")

    def test_reasons_are_counted_and_urls_left_out_of_reasons(self):
        a, b, c = (indexable_page(self.db, p) for p in ("/a/", "/b/", "/c/"))
        audit.regressions(self.db, fetched(a, noindex=1))
        audit.regressions(self.db, fetched(b, status=404))
        audit.regressions(self.db, fetched(c, hops=1, final=f"{BASE}/elsewhere/"))
        msg = group(self.db)[0]["message"]
        self.assertIn("(1 noindex (meta), 1 HTTP 404, 1 redirecting)", msg)
        self.assertNotIn("elsewhere", msg)

    def test_a_page_is_not_counted_twice_and_a_pushed_group_is_left_alone(self):
        u, v = indexable_page(self.db, "/u/"), indexable_page(self.db, "/v/")
        audit.regressions(self.db, fetched(u, noindex=1))
        audit.regressions(self.db, fetched(u, noindex=1))
        self.assertEqual(json.loads(group(self.db)[0]["data"])["n"], 1)
        self.db.execute("UPDATE alerts SET pushed=1")
        audit.regressions(self.db, fetched(u, noindex=1))       # flapping within a week: still not counted again
        audit.regressions(self.db, fetched(v, noindex=1))
        rows = group(self.db)
        self.assertEqual(len(rows), 2, "the dashboard already has the first group; a new one starts")
        self.assertEqual(json.loads(rows[0]["data"])["n"], 1)
        self.assertEqual(json.loads(rows[1]["data"])["n"], 1)
        self.assertIn("/v/", rows[1]["message"])

    def test_sites_are_kept_apart(self):
        u = indexable_page(self.db, "/x/")
        w = indexable_page(self.db, "/x/", site="other.test", base="https://other.test")
        audit.regressions(self.db, fetched(u, noindex=1))
        audit.regressions(self.db, fetched(w, noindex=1))
        self.assertEqual(len(group(self.db)), 1)
        self.assertEqual(len(group(self.db, site="other.test")), 1)

    def test_a_large_share_of_the_site_at_once_is_raised_to_high(self):
        urls = [indexable_page(self.db, f"/p{i}/") for i in range(50)]
        for u in urls[:19]:
            audit.regressions(self.db, fetched(u, noindex=1))
            self.db.execute("UPDATE audits SET noindex=1 WHERE url=?", (u,))
        self.assertEqual(group(self.db)[0]["severity"], "warn")
        audit.regressions(self.db, fetched(urls[19], noindex=1))
        self.assertEqual(group(self.db)[0]["severity"], "high", "20 of 50 pages at once looks like a theme or plugin switch")

    def test_twenty_pages_of_a_big_site_stay_a_warning(self):
        urls = [indexable_page(self.db, f"/p{i}/") for i in range(400)]
        for u in urls[:25]:
            audit.regressions(self.db, fetched(u, noindex=1))
            self.db.execute("UPDATE audits SET noindex=1 WHERE url=?", (u,))
        self.assertEqual(group(self.db)[0]["severity"], "warn")

    def test_pages_indexable_again_are_grouped_good_news(self):
        urls = [indexable_page(self.db, f"/p{i}/") for i in range(4)]
        for u in urls:
            self.db.execute("UPDATE audits SET noindex=1 WHERE url=?", (u,))
            audit.regressions(self.db, fetched(u))
        rows = group(self.db, "pages_unblocked")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["severity"], "good")
        self.assertEqual(rows[0]["message"], "4 pages became indexable again: /p0/, /p1/, /p2/ and 1 more. "
                                             "They go out with the next submission.")

    def test_a_shorter_page_after_the_engines_own_cleanup_raises_nothing(self):
        cleaned, other = indexable_page(self.db, "/cleaned/", words=4000), indexable_page(self.db, "/other/", words=4000)
        self.db.execute("INSERT INTO cleanup_ops(id, site, url, status, applied_at) VALUES('c1', ?, ?, 'applied', ?)",
                        (SITE, cleaned, util.now_iso()))
        audit.regressions(self.db, fetched(cleaned, words=1500))
        audit.regressions(self.db, fetched(other, words=1500))
        shrank = [r["url"] for r in self.db.execute("SELECT url FROM alerts WHERE kind='content_shrank'")]
        self.assertEqual(shrank, [other])

    def test_an_undone_or_old_cleanup_does_not_hide_a_shrink(self):
        u = indexable_page(self.db, "/u/", words=4000)
        self.db.execute("INSERT INTO cleanup_ops(id, site, url, status, applied_at) VALUES('c1', ?, ?, 'undone', ?)",
                        (SITE, u, util.now_iso()))
        self.db.execute("INSERT INTO cleanup_ops(id, site, url, status, applied_at) VALUES('c2', ?, ?, 'applied', "
                        "'2020-01-01T00:00:00+00:00')", (SITE, u))
        audit.regressions(self.db, fetched(u, words=1500))
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM alerts WHERE kind='content_shrank'").fetchone()[0], 1)

    def test_the_stored_list_is_bounded_but_the_count_goes_on(self):
        with patch.object(alerts, "GROUP_KEEP", 5):
            for i in range(9):
                alerts.add_to_group(self.db, SITE, "pages_blocked", "warn", f"{BASE}/p{i}/", "noindex (meta)", audit.BLOCKED_GROUP)
        data = json.loads(group(self.db)[0]["data"])
        self.assertEqual((data["n"], len(data["items"])), (9, 5))
        self.assertIn("9 pages", group(self.db)[0]["message"])

    def test_a_group_is_pushed_like_any_alert(self):
        audit.regressions(self.db, fetched(indexable_page(self.db, "/p/"), noindex=1))

        class Dash:
            sent = []

            def alerts(self, items):
                self.sent += items
        d = Dash()
        self.assertEqual(alerts.push(d, self.db), 1)
        self.assertEqual(d.sent[0]["kind"], "pages_blocked")
        self.assertIsNone(d.sent[0]["url"])
        self.assertLessEqual(len(d.sent[0]["message"]), 1000)


def rows_for(site, n, base=None, bucket="UNKNOWN_TO_GOOGLE"):
    base = base or f"https://{site}"
    rows = [{"url": f"{base}/p{i}/", "kind": "post", "lastmod": None, "last_crawl": None, "gsc_clicks": 0, "gsc_impr": 0,
             "bing_clicks": 0, "bing_impr": 0} for i in range(n)]
    return {"rows": rows, "buckets": {r["url"]: (bucket, []) for r in rows}, "owner": {}}


class PortfolioQueue(unittest.TestCase):
    def setUp(self):
        self.db = memory_db()
        for name, value in (("MANUAL_QUEUE_TOTAL", 10), ("MANUAL_QUEUE_PER_SITE", 10)):
            p = patch.object(config, name, value)
            p.start()
            self.addCleanup(p.stop)

    def test_one_list_of_ten_across_sites_best_expected_gain_first(self):
        results = {s: rows_for(s, 8) for s in ("a.test", "b.test", "c.test")}
        gain = {}
        for k, s in enumerate(results):
            for i, r in enumerate(results[s]["rows"]):
                gain[r["url"]] = 1.0 - 0.1 * i - 0.01 * k          # each site's p0 best; a.test wins ties
        picked = triage.manual_portfolio(self.db, results, gain)
        flat = [m["url"] for s in results for m in picked[s]]
        self.assertEqual(len(flat), 10)
        self.assertEqual({s: len(v) for s, v in picked.items()}, {"a.test": 4, "b.test": 3, "c.test": 3})
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM manual_queue").fetchone()[0], 10, "only picked pages are queued")

    def test_the_per_site_cap_still_holds(self):
        results = {"a.test": rows_for("a.test", 20), "b.test": rows_for("b.test", 20)}
        gain = {r["url"]: (2.0 if s == "a.test" else 1.0) for s in results for r in results[s]["rows"]}
        with patch.object(config, "MANUAL_QUEUE_PER_SITE", 3):
            picked = triage.manual_portfolio(self.db, results, gain)
        self.assertEqual({s: len(v) for s, v in picked.items()}, {"a.test": 3, "b.test": 3})

    def test_demoted_sites_are_skipped_and_zero_turns_the_list_off(self):
        results = {"a.test": rows_for("a.test", 5), "b.test": rows_for("b.test", 5)}
        picked = triage.manual_portfolio(self.db, results, {}, skip={"a.test"})
        self.assertEqual((len(picked["a.test"]), len(picked["b.test"])), (0, 5))
        self.db.execute("DELETE FROM manual_queue")
        with patch.object(config, "MANUAL_QUEUE_TOTAL", 0):
            picked = triage.manual_portfolio(self.db, results, {})
        self.assertEqual(sum(map(len, picked.values())), 0)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM manual_queue").fetchone()[0], 0)

    def test_todays_list_stays_put_on_a_rerun(self):
        results = {"a.test": rows_for("a.test", 15), "b.test": rows_for("b.test", 15)}
        gain = {r["url"]: 1.0 for s in results for r in results[s]["rows"]}
        first = triage.manual_portfolio(self.db, results, gain)
        better = {u: (5.0 if "b.test/p14/" in u else g) for u, g in gain.items()}
        again = triage.manual_portfolio(self.db, results, better)
        self.assertEqual(first, again, "a page already on today's list keeps its place")

    def test_requested_and_thin_pages_stay_off(self):
        res = rows_for("a.test", 3)
        u0, u1, u2 = (r["url"] for r in res["rows"])
        res["owner"] = {u0: {"requested_at": util.now_iso()}}
        res["buckets"][u1] = ("UNKNOWN_TO_GOOGLE", ["THIN"])
        picked = triage.manual_portfolio(self.db, {"a.test": res}, {})
        self.assertEqual([m["url"] for m in picked["a.test"]], [u2])

    def test_one_site_with_many_stale_verdicts_does_not_take_every_slot(self):
        results = {"a.test": rows_for("a.test", 30, bucket="GOOGLE_STALE"), "b.test": rows_for("b.test", 4),
                   "c.test": rows_for("c.test", 4, bucket="DISCOVERED_NOT_INDEXED")}
        picked = triage.manual_portfolio(self.db, results, {})
        self.assertEqual({s: len(v) for s, v in picked.items()}, {"a.test": 4, "b.test": 3, "c.test": 3})
        self.assertEqual(picked["a.test"][0]["bucket"], "GOOGLE_STALE")

    def test_pages_waiting_for_a_scaffolding_cleanup_stay_off(self):
        res = rows_for("a.test", 2)
        u0, u1 = (r["url"] for r in res["rows"])
        res["buckets"][u0] = ("UNKNOWN_TO_GOOGLE", ["TEMPLATE_ARTIFACT"])
        self.assertEqual([m["url"] for m in triage.manual_portfolio(self.db, {"a.test": res}, {})["a.test"]], [u1])

    def test_the_single_site_list_is_unchanged(self):
        res = rows_for("a.test", 14)
        out = triage.manual_queue(self.db, "a.test", res["rows"], res["buckets"], {}, {})
        self.assertEqual(len(out), 10)


if __name__ == "__main__":
    unittest.main()
