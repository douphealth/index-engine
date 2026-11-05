"""Sitemap and feed signals: when to (re)submit, lastmod trust, feed parsing and the fresh-tick readiness check."""
import datetime as dt
import sqlite3
import unittest
from unittest.mock import patch

from engine import config, sitemaps, store
from engine.util import now_iso

SITE = "example.test"
BASE = "https://example.test"


def ago(**kw):
    return (dt.datetime.now(dt.timezone.utc) - dt.timedelta(**kw)).replace(microsecond=0).isoformat()


def database():
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.executescript(store.SCHEMA)
    for sql in store.MIGRATIONS:
        try:
            db.execute(sql)
        except sqlite3.OperationalError:
            pass
    return db


def add_url(db, path, first_seen, lastmod=None, in_sitemap=1):
    db.execute("INSERT INTO urls(url, site, kind, lastmod, in_sitemap, first_seen, last_seen) VALUES(?,?,?,?,?,?,?)",
               (BASE + path, SITE, "post", lastmod, in_sitemap, first_seen, first_seen))


class Response:
    def __init__(self, body, status=200, url=BASE + "/feed/"):
        self.content, self.status_code, self.url = body.encode(), status, url


class DecideTest(unittest.TestCase):
    def setUp(self):
        self.db = database()
        self.feed = BASE + "/sitemap_index.xml"
        add_url(self.db, "/old/", ago(days=60), ago(days=60))

    def decide(self, registered=True, trust=True, fresh=False, engine="google"):
        return sitemaps.decide(self.db, SITE, self.feed, engine, registered, 1, trust, fresh)

    def test_unregistered_is_submitted_then_waits_for_the_engine_to_list_it(self):
        self.assertEqual(self.decide(registered=False), "not registered")
        sitemaps.record(self.db, SITE, self.feed, "google", 1, 200)
        self.assertIsNone(self.decide(registered=False))

    def test_registered_before_the_app_is_recorded_as_baseline(self):
        self.assertIs(self.decide(), sitemaps.BASELINE)

    def test_resubmits_only_after_a_new_or_updated_url(self):
        sitemaps.record(self.db, SITE, self.feed, "google", 1, 200)
        self.db.execute("UPDATE sitemap_submits SET submitted_at=?", (ago(hours=3),))
        self.assertIsNone(self.decide())
        add_url(self.db, "/new/", now_iso())
        self.assertIn("1 new or updated", self.decide())

    def test_rate_limit_applies_even_with_changes(self):
        sitemaps.record(self.db, SITE, self.feed, "google", 1, 200)
        add_url(self.db, "/new/", now_iso())
        self.assertIsNone(self.decide())
        self.assertIsNone(self.decide(fresh=True))

    def test_untrusted_lastmod_is_ignored_but_new_urls_still_count(self):
        sitemaps.record(self.db, SITE, self.feed, "google", 1, 200)
        self.db.execute("UPDATE sitemap_submits SET submitted_at=?", (ago(hours=3),))
        self.db.execute("UPDATE urls SET lastmod=?", (now_iso(),))      # a generation stamp on every URL
        self.assertIsNotNone(self.decide(trust=True))
        self.assertIsNone(self.decide(trust=False))
        add_url(self.db, "/new/", now_iso())
        self.assertIsNotNone(self.decide(trust=False))

    def test_future_lastmod_is_not_a_change(self):
        sitemaps.record(self.db, SITE, self.feed, "google", 1, 200)
        self.db.execute("UPDATE sitemap_submits SET submitted_at=?", (ago(hours=3),))
        self.db.execute("UPDATE urls SET lastmod='2099-01-01'")
        self.assertIsNone(self.decide())

    def test_permission_denied_backs_off_for_days(self):
        sitemaps.record(self.db, SITE, self.feed, "google", 1, 403)
        self.assertIsNone(self.decide(registered=False))
        self.db.execute("UPDATE sitemap_submits SET submitted_at=?", (ago(days=config.SITEMAP_DENIED_RETRY_DAYS + 1),))
        self.assertEqual(self.decide(registered=False), "not registered")

    def test_server_error_retries_after_an_hour(self):
        sitemaps.record(self.db, SITE, self.feed, "google", 1, 500)
        self.assertIsNone(self.decide(registered=False))
        self.db.execute("UPDATE sitemap_submits SET submitted_at=?", (ago(hours=2),))
        self.assertEqual(self.decide(registered=False), "not registered")

    def test_engines_are_tracked_separately(self):
        sitemaps.record(self.db, SITE, self.feed, "google", 1, 200)
        self.assertEqual(self.decide(registered=False, engine="bing"), "not registered")

    def test_rows_written_before_the_engine_column_count_as_google(self):
        self.db.execute("INSERT INTO sitemap_submits(site, feed, submitted_at, url_count, http_status) VALUES(?,?,?,?,?)",
                        (SITE, self.feed, now_iso(), 1, 200))
        self.assertIsNone(self.decide(registered=False))


class SubmitAllTest(unittest.TestCase):
    def test_submits_sitemap_and_working_feed_and_honours_dry_run_and_accepts(self):
        db = database()
        store.set_meta(db, SITE, sitemap_roots=[BASE + "/sitemap_index.xml"], feed={"url": BASE + "/feed/", "ok": True})
        sent = []
        with patch.object(sitemaps, "log"):
            sitemaps.submit_all(db, "bing", SITE, set(), lambda f: sent.append(f) or 200, dry_run=True)
            self.assertEqual(sent, [])
            n = sitemaps.submit_all(db, "bing", SITE, set(), lambda f: sent.append(f) or 200,
                                    accepts=lambda f: not f.endswith("/feed/"))
        self.assertEqual((n, sent), (1, [BASE + "/sitemap_index.xml"]))

    def test_a_failing_submit_is_recorded_and_does_not_raise(self):
        db = database()
        store.set_meta(db, SITE, sitemap_roots=[BASE + "/sitemap_index.xml"])

        def boom(feed):
            raise ConnectionError("down")
        with patch.object(sitemaps, "log"):
            self.assertEqual(sitemaps.submit_all(db, "google", SITE, set(), boom), 0)
        self.assertEqual(db.execute("SELECT http_status FROM sitemap_submits").fetchone()[0], 0)

    def test_broken_feed_is_not_submitted(self):
        self.assertEqual(sitemaps.feeds_for({"sitemap_roots": ["a"], "feed": {"url": "b", "ok": False}}), ["a"])


class LastmodTest(unittest.TestCase):
    def found(self, values):
        return {f"{BASE}/p{i}/": ("post", "sm", v) for i, v in enumerate(values)}

    def test_generation_stamp_is_not_trusted(self):
        h = sitemaps.lastmod_health(self.found(["2026-11-05T06:00:00+00:00"] * 25))
        self.assertFalse(sitemaps.lastmod_trusted({"lastmod_health": h}))
        self.assertTrue(any("same lastmod" in m for _, m in sitemaps.lastmod_messages({"lastmod_health": h})))

    def test_per_post_dates_are_trusted(self):
        h = sitemaps.lastmod_health(self.found([f"2026-10-{d:02d}" for d in range(1, 26)]))
        self.assertTrue(sitemaps.lastmod_trusted({"lastmod_health": h}))
        self.assertEqual(sitemaps.lastmod_messages({"lastmod_health": h}), [])

    def test_stale_cached_sitemap_is_reported(self):
        found = self.found([f"2026-10-{d:02d}T00:00:00" for d in range(1, 26)])
        wp = {u: ("post", "2026-11-01T00:00:00", None) for u in list(found)[:10]}
        h = sitemaps.lastmod_health(found, wp)
        self.assertEqual((h["compared"], h["stale"]), (10, 10))
        self.assertTrue(any("cache" in m for _, m in sitemaps.lastmod_messages({"lastmod_health": h})))

    def test_missing_lastmod_is_reported(self):
        h = sitemaps.lastmod_health(self.found([None] * 30))
        self.assertTrue(any("no lastmod" in m for _, m in sitemaps.lastmod_messages({"lastmod_health": h})))


class FeedTest(unittest.TestCase):
    RSS = ('<?xml version="1.0"?><rss version="2.0" xmlns:atom="http://www.w3.org/2005/Atom"><channel>'
           '<atom:link rel="self" href="https://example.test/feed/"/><atom:link rel="hub" href="https://hub.example/"/>'
           '<item><link>https://example.test/a/</link></item><item><link>https://other.test/b/</link></item></channel></rss>')
    ATOM = ('<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom"><link rel="hub" href="https://hub.example/"/>'
            '<entry><link rel="alternate" href="https://www.example.test/a/"/></entry></feed>')

    def detect(self, body, status=200, url=BASE + "/feed/"):
        with patch.object(sitemaps, "get", return_value=Response(body, status, url)):
            return sitemaps.detect_feed(BASE, "example.test")

    def test_rss_with_hub(self):
        f = self.detect(self.RSS)
        self.assertTrue(f["ok"])
        self.assertEqual((f["items"], f["hubs"], f["links"]), (1, ["https://hub.example/"], [BASE + "/a/"]))
        self.assertNotIn("links", sitemaps.feed_meta(f))

    def test_atom_with_hub(self):
        f = self.detect(self.ATOM)
        self.assertTrue(f["ok"])
        self.assertEqual(f["hubs"], ["https://hub.example/"])

    def test_html_error_page_and_redirects_are_not_feeds(self):
        self.assertFalse(self.detect("<html><body>hi</body></html>")["ok"])
        self.assertFalse(self.detect("", status=404)["ok"])
        self.assertFalse(self.detect(self.RSS, url="https://elsewhere.test/feed/")["ok"])

    def test_feed_messages(self):
        self.assertEqual(sitemaps.feed_messages({"wp": True, "feed": {"ok": True, "hubs": ["h"]}}), [])
        self.assertTrue(sitemaps.feed_messages({"wp": True, "feed": {"ok": True, "hubs": []}}))
        self.assertTrue(sitemaps.feed_messages({"wp": True, "feed": {"ok": False, "url": "u", "why": "HTTP 404"}}))
        self.assertEqual(sitemaps.feed_messages({"wp": False, "feed": {"ok": False}}), [])


class FreshReadyTest(unittest.TestCase):
    def setUp(self):
        self.db = database()
        store.set_meta(self.db, SITE, base=BASE, sitemap_roots=[BASE + "/sitemap_index.xml"],
                       feed={"url": BASE + "/feed/", "ok": True, "hubs": ["https://hub.example/"]})
        self.changes = {SITE: [(BASE + "/new/", "post", "2026-11-05T10:00:00+00:00", None)]}

    def ready(self, found, feed_links):
        with patch("engine.discover.walk_sitemaps", return_value=(found, [], [])), \
             patch.object(sitemaps, "detect_feed", return_value={"ok": True, "links": feed_links}), \
             patch.object(sitemaps, "log"):
            return sitemaps.fresh_ready(self.db, self.changes)

    def test_cached_sitemap_without_the_post_is_not_resubmitted(self):
        self.assertEqual(self.ready({}, []), {})

    def test_sitemap_listing_an_older_version_is_not_ready(self):
        found = {BASE + "/new/": ("post", "sm", "2026-11-01T00:00:00+00:00")}
        self.assertEqual(self.ready(found, []), {})

    def test_listed_post_makes_sitemap_and_feed_ready(self):
        found = {BASE + "/new/": ("post", "sm", "2026-11-05T10:00:00+00:00")}
        self.assertEqual(self.ready(found, [BASE + "/new/"]), {SITE: {BASE + "/sitemap_index.xml", BASE + "/feed/"}})

    def test_feed_alone_can_be_ready(self):
        self.assertEqual(self.ready({}, [BASE + "/new/"]), {SITE: {BASE + "/feed/"}})


class PingHubsTest(unittest.TestCase):
    def test_no_hub_no_ping_and_dry_run_sends_nothing(self):
        db = database()
        store.set_meta(db, SITE, feed={"url": BASE + "/feed/", "ok": True, "hubs": []})
        with patch.object(sitemaps, "http") as http:
            self.assertEqual(sitemaps.ping_hubs(db, [SITE]), 0)
            store.set_meta(db, SITE, feed={"url": BASE + "/feed/", "ok": True, "hubs": ["https://hub.example/"]})
            with patch.object(sitemaps, "log"):
                self.assertEqual(sitemaps.ping_hubs(db, [SITE], dry_run=True), 0)
            http.assert_not_called()

    def test_failed_ping_does_not_move_the_marker(self):
        db = database()
        store.set_meta(db, SITE, feed={"url": BASE + "/feed/", "ok": True, "hubs": ["https://hub.example/"]})
        with patch.object(sitemaps, "http") as http, patch.object(sitemaps, "log"):
            http.return_value.post.return_value.status_code = 503
            self.assertEqual(sitemaps.ping_hubs(db, [SITE]), 0)
        self.assertIsNone(store.get_meta(db, SITE).get("websub_at"))


class SignalFreshTest(unittest.TestCase):
    def test_only_ready_feeds_reach_the_engines_and_failures_never_break_the_tick(self):
        import indexengine as ie
        ready = {SITE: {BASE + "/feed/"}}
        with patch.object(ie.sitemaps, "fresh_ready", return_value=ready), \
             patch.object(ie.google, "sync_sitemaps") as g, patch.object(ie.bing, "sync_feeds") as b, \
             patch.object(ie.sitemaps, "ping_hubs") as w:
            ie.signal_fresh("db", {SITE: []}, "gc", {SITE: "p"}, "bc", {SITE: "b"})
        self.assertEqual(g.call_args.kwargs["only"], ready)
        self.assertEqual(b.call_args.kwargs["only"], ready)
        self.assertEqual(w.call_args.kwargs["only"], ready)
        with patch.object(ie.sitemaps, "fresh_ready", side_effect=RuntimeError("network")), patch.object(ie, "log") as lg:
            ie.signal_fresh("db", {SITE: []}, None, {}, None, {})
        self.assertIn("fresh signals failed", lg.call_args.args[0])


if __name__ == "__main__":
    unittest.main()
