"""Traffic history, Bing index health and revival analysis: statistics, pulls, classification and alerts. Offline."""
import datetime as dt
import tempfile
import unittest
from pathlib import Path

from engine import alerts, revival, store, traffic

TODAY = dt.date(2026, 10, 28)


def bing_day(d):
    return f"/Date({int(dt.datetime(d.year, d.month, d.day, tzinfo=dt.timezone.utc).timestamp() * 1000)})/"


class FakeBing:
    def __init__(self, rank=(), crawl=()):
        self.rank, self.crawl = list(rank), list(crawl)

    def rank_traffic(self, url):
        return self.rank

    def crawl_stats(self, url):
        return self.crawl


class FakeGSC:
    def __init__(self, rows=()):
        self.rows, self.calls = list(rows), []

    def search_dates(self, prop, start, end):
        self.calls.append((prop, start, end))
        return [r for r in self.rows if str(start) <= r["keys"][0] <= str(end)]


def days(n, end=dt.date(2026, 10, 25)):
    return [end - dt.timedelta(days=k) for k in range(n - 1, -1, -1)]


def fresh_db(tmp):
    return store.connect(Path(tmp) / "t.db")


class StatisticsTests(unittest.TestCase):
    def test_windows_are_exact_and_adjacent(self):
        (n0, n1), (p0, p1) = traffic.windows("2026-10-25")
        self.assertEqual((n0, n1), ("2026-09-28", "2026-10-25"))
        self.assertEqual((p0, p1), ("2026-08-31", "2026-09-27"))
        self.assertEqual((dt.date.fromisoformat(n1) - dt.date.fromisoformat(n0)).days + 1, traffic.SPAN)

    def test_clear_large_drop_is_declining_with_high_confidence(self):
        v = traffic.compare(695, 1037)
        self.assertEqual((v["verdict"], v["confidence"]), ("declining", "high"))
        self.assertAlmostEqual(v["pct"], -33.0, places=1)

    def test_big_but_noisy_change_is_not_called(self):
        # 100 -> 70 is -30% but only about 2.3 standard errors: stable, not declining.
        self.assertEqual(traffic.compare(70, 100)["verdict"], "stable")

    def test_clear_but_small_change_is_not_called(self):
        self.assertEqual(traffic.compare(950, 1000)["verdict"], "stable")      # -5%: below the 25% bar however many clicks

    def test_tiny_numbers_are_too_little_data(self):
        self.assertEqual(traffic.compare(20, 39)["verdict"], "too_little_data")
        self.assertEqual(traffic.compare(0, 0)["verdict"], "too_little_data")

    def test_growth_is_called_symmetrically(self):
        v = traffic.compare(360, 277)
        self.assertEqual(v["verdict"], "growing")
        self.assertEqual(v["confidence"], "medium")


class PullAndReadTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = fresh_db(self.tmp.name)

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def bing_series(self, per_day_old, per_day_new):
        rows = []
        for d in days(56):
            clicks = per_day_old if d <= dt.date(2026, 9, 27) else per_day_new
            rows.append({"Date": bing_day(d), "Clicks": clicks, "Impressions": clicks * 100})
        return rows

    def test_pull_stores_history_and_is_idempotent(self):
        b = FakeBing(self.bing_series(20, 10), [{"Date": bing_day(dt.date(2026, 10, 25)), "CrawledPages": 100, "InIndex": 500, "CrawlErrors": 3,
                                                  "Code4xx": 2, "Code5xx": 1, "BlockedByRobotsTxt": 4, "ConnectionTimeout": 0, "DnsFailures": 0, "InLinks": 9}])
        traffic.pull(self.db, None, {}, b, {"a.example": "https://a.example/"}, today=TODAY)
        traffic.pull(self.db, None, {}, b, {"a.example": "https://a.example/"}, today=TODAY)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM traffic_daily").fetchone()[0], 56)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM bing_crawl").fetchone()[0], 1)
        t = traffic.site_traffic(self.db, "a.example")
        self.assertEqual((t["bing"]["clicks_prev"], t["bing"]["clicks_now"]), (20 * 28, 10 * 28))
        self.assertEqual(t["bing"]["clicks"]["verdict"], "declining")
        self.assertTrue(t["bing"]["enough_history"])
        self.assertIsNone(t["google"])
        self.assertEqual(len(t["series"]), 120)
        self.assertEqual(t["series"][-1][0], "2026-10-25")
        self.assertEqual(t["series"][0][1], 0)                                  # days before the history are zero, not missing

    def test_google_is_read_fully_once_then_only_recently(self):
        rows = [{"keys": [d.isoformat()], "clicks": 3, "impressions": 30} for d in days(200, end=dt.date(2026, 10, 26))]
        g = FakeGSC(rows)
        traffic.pull(self.db, g, {"a.example": "sc-domain:a.example"}, None, {}, today=TODAY)
        traffic.pull(self.db, g, {"a.example": "sc-domain:a.example"}, None, {}, today=TODAY)
        first, second = g.calls
        self.assertEqual((TODAY - first[1]).days, 480)
        self.assertEqual((dt.date.fromisoformat("2026-10-26") - second[1]).days, 60)
        self.assertEqual(second[2], TODAY - dt.timedelta(days=2))

    def test_a_short_history_never_produces_a_trend(self):
        rows = [{"Date": bing_day(d), "Clicks": 5 if d < dt.date(2026, 10, 1) else 80, "Impressions": 500} for d in days(40)]
        traffic.pull(self.db, None, {}, FakeBing(rows), {"a.example": "https://a.example/"}, today=TODAY)
        t = traffic.site_traffic(self.db, "a.example")["bing"]
        self.assertFalse(t["enough_history"])
        self.assertEqual(t["clicks"]["verdict"], "too_little_data")      # 28 days would look like a 16x rise; it is only a young history
        self.assertIn("40 days", t["clicks"]["reason"])

    def test_failed_source_does_not_stop_the_others(self):
        class Boom(FakeBing):
            def rank_traffic(self, url):
                raise RuntimeError("HTTP 500")
        g = FakeGSC([{"keys": ["2026-10-20"], "clicks": 1, "impressions": 2}])
        traffic.pull(self.db, g, {"a.example": "sc-domain:a.example"}, Boom(), {"a.example": "https://a.example/"}, today=TODAY)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM traffic_daily WHERE src='google'").fetchone()[0], 1)

    def test_old_rows_are_pruned(self):
        self.db.execute("INSERT INTO traffic_daily VALUES('a.example','2024-01-01','bing',5,50)")
        traffic.pull(self.db, None, {}, FakeBing(), {"a.example": "https://a.example/"}, today=TODAY)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM traffic_daily").fetchone()[0], 0)


class BingHealthTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = fresh_db(self.tmp.name)
        for i in range(100):
            self.db.execute("INSERT INTO urls(url, site, in_sitemap) VALUES(?,?,1)", (f"https://a.example/p{i}/", "a.example"))

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def crawl(self, **last):
        for k in range(40, -1, -1):
            d = (dt.date(2026, 10, 25) - dt.timedelta(days=k)).isoformat()
            row = dict(crawled=100, in_index=100 if k >= 30 else 110, errors=0, c4xx=0, c5xx=0, robots=0, timeouts=0, dns=0, in_links=0)
            if k < 7:
                row.update(last)
            self.db.execute("INSERT INTO bing_crawl VALUES(?,?,?,?,?,?,?,?,?,?,?)", ("a.example", d, row["crawled"], row["in_index"], row["errors"],
                            row["c4xx"], row["c5xx"], row["robots"], row["timeouts"], row["dns"], row["in_links"]))

    def kinds(self):
        return {f["kind"] for f in traffic.bing_health(self.db, "a.example")["findings"]}

    def test_no_report_gives_none(self):
        self.assertIsNone(traffic.bing_health(self.db, "a.example"))

    def test_healthy_site_has_no_findings(self):
        self.crawl()
        h = traffic.bing_health(self.db, "a.example")
        self.assertEqual(h["findings"], [])
        self.assertEqual((h["in_index"], h["known_urls"], h["ratio"]), (110, 100, 1.1))

    def test_index_bloat_needs_both_size_and_ratio(self):
        self.crawl()
        self.db.execute("UPDATE bing_crawl SET in_index=1200")
        self.assertIn("index_bloat", self.kinds())                      # 1,200 pages for 100 URLs
        self.db.execute("UPDATE bing_crawl SET in_index=400")
        self.assertNotIn("index_bloat", self.kinds())                   # a 4x ratio on a small index is not bloat
        self.db.execute("UPDATE bing_crawl SET in_index=900")
        self.assertNotIn("index_bloat", self.kinds())                   # 9x, but under 1,000 pages: not worth an alarm

    def test_growth_over_30_days_is_measured(self):
        self.crawl()
        self.assertEqual(traffic.bing_health(self.db, "a.example")["growth_pct"], 10.0)

    def test_server_errors_use_a_rate_not_a_count(self):
        self.crawl(crawled=30000, c5xx=60)                              # 60 over a week of 30,000 pages crawled each day: 0.03%
        self.assertNotIn("server_errors", self.kinds())
        self.db.execute("DELETE FROM bing_crawl")
        self.crawl(crawled=100, c5xx=10)                                # 70 over 700 pages: 10%
        self.assertIn("server_errors", self.kinds())

    def test_robots_and_missing_pages(self):
        self.crawl(crawled=100, robots=100, c4xx=10)
        self.assertEqual(self.kinds() & {"robots_blocked", "missing_pages"}, {"robots_blocked", "missing_pages"})


class RevivalTests(unittest.TestCase):
    SITE = "a.example"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = fresh_db(self.tmp.name)
        self.db.execute("INSERT INTO traffic_daily VALUES(?,?,?,?,?)", (self.SITE, "2026-10-25", "bing", 1, 10))

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def page(self, path, bucket, now, prev, flags="", reason="", listed=True, title=None):
        url = f"https://a.example{path}"
        if listed:
            self.db.execute("INSERT INTO urls(url, site, in_sitemap, lastmod) VALUES(?,?,1,'2026-09-12T00:00:00+00:00')", (url, self.SITE))
            self.db.execute("INSERT INTO triage(url, site, bucket, flags, reason) VALUES(?,?,?,?,?)", (url, self.SITE, bucket, flags, reason))
            self.db.execute("INSERT OR REPLACE INTO audits(url, title) VALUES(?,?)", (url, title or path))
        self.db.execute("INSERT INTO page_trend(url, site, b_now, b_prev) VALUES(?,?,?,?)", (url, self.SITE, now, prev))
        return url

    def test_losses_are_split_by_what_is_behind_them(self):
        self.page("/live/", "CRAWLED_NOT_INDEXED", 10, 40)
        self.page("/blocked/", "NOINDEX", 0, 20, reason="noindex via meta")
        gone = self.page("/gone/", None, 0, 6, listed=False)
        moved = self.page("/old/", None, 0, 5, listed=False)
        self.page("/new/", "INDEXED", 9, 1)
        self.db.execute("INSERT INTO trend_probe VALUES(?,404,'',datetime('now'))", (gone,))
        self.db.execute("INSERT INTO trend_probe VALUES(?,301,'https://a.example/new/',datetime('now'))", (moved,))
        r = revival.analyze(self.db, self.SITE)
        lost = {k: v["clicks_lost"] for k, v in r["lost_by_kind"].items()}
        self.assertEqual(lost, {"live": 30, "blocked": 20, "gone": 6, "moved": 5})
        self.assertEqual(r["clicks_gained"], 8)
        top = {x["url"].rsplit("/", 2)[1]: x for x in r["losers"]}
        self.assertEqual(top["live"]["delta"], -30)
        self.assertIn("noindex via meta", top["blocked"]["todo"])
        self.assertIn("404", top["gone"]["todo"])
        self.assertIn("(that page earned 1 clicks before and 9 now)", top["old"]["todo"])
        self.assertEqual([x["url"].rsplit("/", 2)[1] for x in r["losers"]][:2], ["live", "blocked"])    # biggest loss first

    def test_a_home_page_is_not_told_to_get_article_links(self):
        self.page("/", "INDEXED", 0, 42, flags="ORPHAN")
        loser = revival.analyze(self.db, self.SITE)["losers"][0]
        self.assertEqual(loser["diagnosis"], "home")
        self.assertNotIn("contextual links", loser["todo"])

    def test_flags_choose_the_advice(self):
        self.page("/orphan/", "CRAWLED_NOT_INDEXED", 0, 5, flags="ORPHAN")
        self.page("/thin/", "CRAWLED_NOT_INDEXED", 0, 4, flags="THIN")
        self.page("/plain/", "INDEXED", 0, 3)
        todo = {x["url"].rsplit("/", 2)[1]: x["todo"] for x in revival.analyze(self.db, self.SITE)["losers"]}
        self.assertIn("contextual links", todo["orphan"])
        self.assertIn("more substantial", todo["thin"])
        self.assertIn("rankings or demand", todo["plain"])

    def test_small_losses_are_counted_but_not_listed(self):
        self.page("/tiny/", "INDEXED", 9, 10)
        r = revival.analyze(self.db, self.SITE)
        self.assertEqual((r["clicks_lost"], r["losers"]), (1, []))

    def test_coverage_says_how_much_of_the_site_the_pages_explain(self):
        self.db.execute("DELETE FROM traffic_daily")
        for d in (dt.date(2026, 10, 25), dt.date(2026, 9, 1)):
            self.db.execute("INSERT INTO traffic_daily VALUES(?,?,?,?,?)", (self.SITE, d.isoformat(), "bing", 100, 1000))
        self.db.execute("INSERT INTO traffic_daily VALUES(?,?,?,?,?)", (self.SITE, "2026-08-31", "bing", 0, 0))
        self.page("/a/", "INDEXED", 80, 50)
        r = revival.analyze(self.db, self.SITE)
        self.assertEqual(r["coverage"], {"now": 0.8, "prev": 0.5})

    def test_pages_served_at_two_addresses_are_a_finding(self):
        for i in range(4):
            listed = self.page(f"/p{i}/", "INDEXED", 5, 5)
            v = listed.rstrip("/")
            self.db.execute("INSERT INTO url_variants VALUES(?,?,?)", (v, self.SITE, listed))
            self.db.execute("INSERT INTO trend_probe VALUES(?,200,'',datetime('now'))", (v,))
        self.db.execute("INSERT INTO url_variants VALUES('https://a.example/x','a.example','https://a.example/x/')")
        f = revival.analyze(self.db, self.SITE)["findings"]
        self.assertEqual([x["kind"] for x in f], ["duplicate_urls"])
        self.assertEqual(f[0]["count"], 4)
        self.assertIn("checked 4 of 5 so far", f[0]["message"])

    def test_a_relative_redirect_target_is_matched_to_its_page(self):
        old = self.page("/old/", None, 0, 5, listed=False)
        self.page("/new/", "INDEXED", 9, 1)
        self.db.execute("INSERT INTO trend_probe VALUES(?,301,'/new/',datetime('now'))", (old,))
        top = {x["url"].rsplit("/", 2)[1]: x for x in revival.analyze(self.db, self.SITE)["losers"]}
        self.assertIn("(that page earned 1 clicks before and 9 now)", top["old"]["todo"])

    def test_headline_states_only_what_is_clear(self):
        tr = traffic.site_traffic(self.db, self.SITE)
        self.assertIn("Too little traffic", revival.headline(self.SITE, tr, {}, None))
        tr["bing"].update(clicks_prev=1037, clicks_now=695, clicks=traffic.compare(695, 1037))
        self.assertEqual(revival.headline(self.SITE, tr, {}, None), "Bing clicks fell 33% (1,037 to 695)")


class AliasAndProbeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = fresh_db(self.tmp.name)
        self.db.execute("INSERT INTO urls(url, site) VALUES('https://a.example/post/','a.example')")
        self.db.execute("INSERT INTO urls(url, site) VALUES('https://a.example/other','a.example')")

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def test_variants_map_onto_the_listed_spelling_and_are_remembered(self):
        alias, seen = traffic.aliaser(self.db, "a.example")
        self.assertEqual(alias("https://a.example/post"), "https://a.example/post/")
        self.assertEqual(alias("https://a.example/other/"), "https://a.example/other")
        self.assertEqual(alias("https://a.example/post/"), "https://a.example/post/")      # already listed: untouched
        self.assertEqual(alias("https://a.example/unknown"), "https://a.example/unknown")  # not a variant of anything
        self.assertEqual(seen, {"https://a.example/post": "https://a.example/post/", "https://a.example/other/": "https://a.example/other"})

    def test_probe_asks_once_a_week_and_records_what_it_sees(self):
        self.db.execute("INSERT INTO page_trend(url, site, b_now, b_prev) VALUES('https://a.example/gone','a.example',0,5)")
        self.db.execute("INSERT INTO url_variants VALUES('https://a.example/post','a.example','https://a.example/post/')")
        asked = []

        class Resp:
            def __init__(self, code, loc=""):
                self.status_code, self.headers = code, {"Location": loc} if loc else {}

        class Session:
            def get(self, url, **kw):
                asked.append((url, kw.get("allow_redirects")))
                return Resp(404) if url.endswith("gone") else Resp(301, "https://a.example/post/")

        self.db.execute("INSERT INTO page_trend(url, site, b_now, b_prev) VALUES('https://elsewhere.example/x','a.example',0,9)")
        revival.probe_missing(self.db, ["a.example"], session=Session())
        revival.probe_missing(self.db, ["a.example"], session=Session())
        self.assertNotIn("https://elsewhere.example/x", [u for u, _ in asked])                                   # never another host
        self.assertEqual(sorted(u for u, _ in asked), ["https://a.example/gone", "https://a.example/post"])   # the second pass asked nothing
        self.assertTrue(all(redirects is False for _, redirects in asked))                                       # never follows redirects
        self.assertEqual({r["url"][-4:]: r["status"] for r in self.db.execute("SELECT url, status FROM trend_probe")}, {"gone": 404, "post": 301})


class AlertTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = fresh_db(self.tmp.name)

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def results(self, now=695, prev=1037, findings=()):
        tr = {"bing": {"end": "2026-10-25", "clicks_now": now, "clicks_prev": prev, "clicks": traffic.compare(now, prev)}, "google": None}
        return {"a.example": {"traffic": tr, "health": {"findings": list(findings)}, "revival": {"findings": []}}}

    def kinds(self):
        return sorted(r["kind"] for r in self.db.execute("SELECT kind FROM alerts"))

    def test_a_clear_drop_alerts_once_a_week_and_a_rise_is_good_news(self):
        revival.raise_alerts(self.db, self.results())
        revival.raise_alerts(self.db, self.results())
        self.assertEqual(self.kinds(), ["traffic_drop_bing"])
        sev = self.db.execute("SELECT severity FROM alerts").fetchone()[0]
        self.assertEqual(sev, "warn")                                       # -33%: not the -40% needed for "high"
        revival.raise_alerts(self.db, self.results(now=360, prev=277))
        self.assertIn("traffic_up_bing", self.kinds())

    def test_a_severe_drop_on_a_real_site_is_high(self):
        revival.raise_alerts(self.db, self.results(now=400, prev=1000))
        self.assertEqual(self.db.execute("SELECT severity FROM alerts").fetchone()[0], "high")

    def test_paused_sites_get_no_alerts(self):
        revival.raise_alerts(self.db, self.results(), only={"other.example"})
        self.assertEqual(self.kinds(), [])

    def test_stable_or_tiny_traffic_raises_nothing(self):
        revival.raise_alerts(self.db, self.results(now=980, prev=1000))
        revival.raise_alerts(self.db, self.results(now=3, prev=9))
        self.assertEqual(self.kinds(), [])

    def test_chronic_bing_findings_repeat_monthly_not_weekly(self):
        f = [{"kind": "index_bloat", "severity": "warn", "message": "bloat"}]
        revival.raise_alerts(self.db, self.results(now=1000, prev=1000, findings=f))
        self.db.execute("UPDATE alerts SET at=datetime('now','-10 days')")
        revival.raise_alerts(self.db, self.results(now=1000, prev=1000, findings=f))
        self.assertEqual(self.kinds(), ["bing_index_bloat"])               # 10 days later: still suppressed
        self.db.execute("UPDATE alerts SET at=datetime('now','-31 days')")
        revival.raise_alerts(self.db, self.results(now=1000, prev=1000, findings=f))
        self.assertEqual(self.kinds(), ["bing_index_bloat", "bing_index_bloat"])


if __name__ == "__main__":
    unittest.main()
