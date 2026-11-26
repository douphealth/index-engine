"""Sitemap changes are judged before they alarm, and an audit cannot flood or keep hammering a failing site.

Both rules come from 25-26 Nov 2026: openclaw-skillshub.com's sitemap fell from 160 to 89 URLs because 71 pages were
deliberately set to noindex (the engine raised a high alert), and the first audit of tenderatlas.shop fetched 3,313
pages in ten minutes, ran that site's database out of its daily allowance and recorded 2,102 pages as 404."""
import datetime as dt
import sqlite3
import unittest
from unittest.mock import patch

from engine import audit, config, discover, store

SITE = "example.test"
BASE = "https://" + SITE
NS = "http://www.sitemaps.org/schemas/sitemap/0.9"


def iso(days_ago=0.0):
    return (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days_ago)).isoformat(timespec="seconds")


def memory_db():
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.executescript(store.SCHEMA)
    for migration in store.MIGRATIONS:
        db.execute(migration)
    return db


def alerts_of(db):
    return [(r["kind"], r["severity"], r["message"]) for r in db.execute("SELECT kind, severity, message FROM alerts ORDER BY id")]


class Unstable(unittest.TestCase):
    def hist(self, *counts, hours=6):
        n = len(counts)
        return [[iso((n - i) * hours / 24), c] for i, c in enumerate(counts)]

    def test_back_and_forth_is_unstable(self):
        self.assertTrue(discover.unstable(self.hist(3313, 86, 3313, 86)))
        self.assertTrue(discover.unstable(self.hist(86, 3313, 86, 3313, 3313)))

    def test_one_drop_or_one_jump_is_not(self):
        self.assertFalse(discover.unstable(self.hist(160, 160, 89, 89)))
        self.assertFalse(discover.unstable(self.hist(86, 3313, 3313, 3313)))
        self.assertFalse(discover.unstable(self.hist(160, 89, 99)))          # down, then a little up: under the swing size

    def test_small_wobble_is_not(self):
        self.assertFalse(discover.unstable(self.hist(500, 505, 498, 507, 499)))

    def test_old_readings_do_not_count(self):
        history = [[iso(30), 3313], [iso(29), 86], [iso(28), 3313], [iso(0.1), 3313]]
        self.assertFalse(discover.unstable(history, history[-1][0]))


class JudgeSitemap(unittest.TestCase):
    def setUp(self):
        self.db = memory_db()
        self.addCleanup(self.db.close)

    def judge(self, before, now, removed_states, history=None):
        found = {f"{BASE}/kept-{i}/": ("post", "sm", None) for i in range(now)}
        removed = [f"{BASE}/gone-{i}/" for i in range(max(0, before - now))]
        states = iter(removed_states)
        with patch.object(discover, "page_state", side_effect=lambda url, rp: next(states)) as fetched:
            verdict = discover.judge_sitemap(self.db, SITE, before, found, removed, history or [[iso(1), before], [iso(0), now]])
        return verdict, fetched.call_count

    def test_removed_pages_that_are_noindex_or_gone_is_information_not_an_alarm(self):
        verdict, fetched = self.judge(160, 89, [False] * 12)
        self.assertEqual(verdict, "pruned")
        self.assertEqual(fetched, 12)                       # a sample, never all 71
        (kind, severity, message), = alerts_of(self.db)
        self.assertEqual((kind, severity), ("sitemap_pruned", "info"))
        self.assertIn("71 URLs left the sitemaps (160 to 89)", message)
        self.assertIn("12 of the 12 checked", message)

    def test_removed_pages_that_still_answer_is_a_high_alert_that_says_so(self):
        verdict, _ = self.judge(160, 89, [True] * 10 + [False] * 2)
        self.assertEqual(verdict, "shrank")
        (kind, severity, message), = alerts_of(self.db)
        self.assertEqual((kind, severity), ("sitemap_shrank", "high"))
        self.assertIn("10 of the 12 dropped pages checked still answer and are indexable", message)

    def test_pages_that_cannot_be_judged_do_not_make_it_look_deliberate(self):
        verdict, _ = self.judge(160, 89, [None] * 12)        # the site is down: nothing is known
        self.assertEqual(verdict, "shrank")
        self.assertEqual(alerts_of(self.db)[0][1], "high")

    def test_swinging_sitemap_is_reported_as_unstable_without_fetching_anything(self):
        history = [[iso(0.75), 3313], [iso(0.5), 86], [iso(0.25), 3313], [iso(0), 86]]
        verdict, fetched = self.judge(3313, 86, [], history)
        self.assertEqual((verdict, fetched), ("unstable", 0))
        (kind, severity, message), = alerts_of(self.db)
        self.assertEqual((kind, severity), ("sitemap_unstable", "high"))
        self.assertIn("3,313, 86, 3,313, 86", message)

    def test_sudden_growth_is_flagged(self):
        verdict, _ = self.judge(86, 3313, [])
        self.assertEqual(verdict, "grew")
        self.assertEqual(alerts_of(self.db)[0][:2], ("sitemap_grew", "warn"))

    def test_ordinary_change_raises_nothing(self):
        for before, now in ((160, 150), (160, 200), (10, 2), (86, 250)):
            self.assertIsNone(self.judge(before, now, [])[0], (before, now))
        self.assertEqual(alerts_of(self.db), [])

    def test_sample_is_spread_over_the_removed_urls(self):
        urls = [f"{BASE}/{c}/{i:03d}/" for c in "abc" for i in range(100)]
        sample = discover.spread(urls, 12)
        self.assertEqual(len(sample), 12)
        self.assertEqual({u.split("/")[3] for u in sample}, {"a", "b", "c"})
        self.assertEqual(discover.spread(urls[:5], 12), sorted(urls[:5]))


class Response:
    def __init__(self, body="", status=200):
        self.status_code, self.text, self.content, self.headers = status, body, body.encode(), {"Content-Type": "application/xml"}


class DiscoverKeepsHistory(unittest.TestCase):
    """End to end through discover.run: readings are remembered only when complete, and a prune is judged once."""

    def setUp(self):
        self.db = memory_db()
        self.addCleanup(self.db.close)

    def scan(self, urls, status=200):
        body = f'<urlset xmlns="{NS}">' + "".join(f"<url><loc>{u}</loc></url>" for u in urls) + "</urlset>"

        def get(url, **kw):
            if url.endswith("/robots.txt"):
                return Response(f"Sitemap: {BASE}/sitemap.xml")
            return Response(body, status)
        with patch.object(discover, "get", side_effect=get), patch.object(discover.config, "site_base", return_value=BASE), \
                patch.object(discover, "page_state", return_value=False) as fetched:
            discover.run(self.db, [SITE], use_wp=False)
        return fetched.call_count

    def test_prune_then_outage(self):
        all_urls = [f"{BASE}/p{i}/" for i in range(160)]
        self.assertEqual(self.scan(all_urls), 0)
        self.assertEqual(self.scan(all_urls[:89]), 12)
        self.assertEqual([a[0] for a in alerts_of(self.db)], ["sitemap_pruned"])
        meta = store.get_meta(self.db, SITE)
        self.assertEqual([c for _, c in meta["sitemap_history"]], [160, 89])
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM urls WHERE in_sitemap=1").fetchone()[0], 89)
        # The sitemap answers 503 (the site is down): nothing is compared, forgotten or remembered.
        self.assertEqual(self.scan([], status=503), 0)
        meta = store.get_meta(self.db, SITE)
        self.assertEqual([c for _, c in meta["sitemap_history"]], [160, 89])
        self.assertEqual(meta["sitemap_count"], 89)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM urls WHERE in_sitemap=1").fetchone()[0], 89)
        self.assertEqual(len(alerts_of(self.db)), 1)


class DueRows(unittest.TestCase):
    def setUp(self):
        self.db = memory_db()
        self.addCleanup(self.db.close)

    def page(self, name, checked_days=None, status=200, in_sitemap=1, error=None, site=SITE):
        url = f"https://{site}/{name}/"
        self.db.execute("INSERT INTO urls(url, site, in_sitemap, in_wp) VALUES (?,?,?,0)", (url, site, in_sitemap))
        if checked_days is not None:
            self.db.execute("INSERT INTO audits(url, checked_at, status, error) VALUES (?,?,?,?)", (url, iso(checked_days), status, error))
        return url

    def due(self, **kw):
        return [u for _, u, _, _ in audit.due_rows(self.db, [SITE], **kw).get(SITE, [])]

    def test_listed_page_that_answered_404_is_fetched_again_after_a_day_not_a_week(self):
        yesterday = self.page("gone-yesterday", 1.2, 404)
        self.page("gone-an-hour-ago", 0.05, 404)
        self.page("fine", 1.2, 200)
        self.page("forbidden-but-unlisted", 1.2, 403, in_sitemap=0)      # not in any sitemap (and not WP): not tracked
        self.assertEqual(self.due(), [yesterday])

    def test_cap_takes_never_fetched_first_then_longest_unchecked_and_reports_the_rest(self):
        new = [self.page(f"new-{i}") for i in range(3)]
        old = self.page("old", 30)
        older = self.page("older", 40)
        held = {}
        with patch.object(config, "AUDIT_MAX_PER_SITE", 4), patch.object(config, "AUDIT_SITE_MAX", {}):
            got = [u for _, u, _, _ in audit.due_rows(self.db, [SITE], held=held)[SITE]]
        self.assertEqual(got, new + [older])
        self.assertEqual(held, {SITE: 1})
        self.assertNotIn(old, got)

    def test_site_override_explicit_max_and_force(self):
        for i in range(10):
            self.page(f"p{i}")
        with patch.object(config, "AUDIT_MAX_PER_SITE", 8), patch.object(config, "AUDIT_SITE_MAX", {SITE: 3}):
            self.assertEqual(len(self.due()), 3)
            self.assertEqual(len(self.due(max_per_site=5)), 5)
            self.assertEqual(len(self.due(force=True)), 10)

    def test_default_cap_is_generous_enough_for_the_ordinary_sites(self):
        self.assertGreaterEqual(config.AUDIT_MAX_PER_SITE, 500)
        self.assertLessEqual(config.AUDIT_SITE_MAX["tenderatlas.shop"], 200)


class StopsAFailingSite(unittest.TestCase):
    def setUp(self):
        self.db = memory_db()
        self.addCleanup(self.db.close)

    def run_audit(self, statuses_by_site):
        """statuses_by_site: {site: [status of its 1st, 2nd, ... page]}; returns the URLs fetched, per site."""
        fetched = {s: [] for s in statuses_by_site}
        for site, statuses in statuses_by_site.items():
            for i in range(len(statuses)):
                self.db.execute("INSERT INTO urls(url, site, in_sitemap, in_wp) VALUES (?,?,1,0)", (f"https://{site}/p{i:03d}/", site))

        def fetch_page(url, rp):
            site = url.split("/")[2]
            fetched[site].append(url)
            status = statuses_by_site[site][int(url.split("/p")[1].rstrip("/"))]
            return {"url": url, "checked_at": iso(), "status": status, "final_url": url, "hops": 0, "ctype": "text/html", "ms": 50,
                    "noindex": int(status == 404), "noindex_src": None, "canonical": None, "canonical_state": None, "title": "t",
                    "h1": "", "words": 400, "sketch": "[]", "out_links": 0, "error": None, "trust": None, "links": [], "bytes": 1,
                    "inline_b64": None, "description": None, "jsonld": None, "jsonld_errors": None, "robots_google": 1,
                    "robots_bing": 1, "retried": None}
        # One request in flight per site, so the order of answers is the order of the queue.
        with patch.object(audit, "fetch_page", side_effect=fetch_page), patch.object(config, "PER_HOST_CONCURRENCY", 1), \
                patch.object(config, "PER_HOST_MAX", 1), patch.object(config, "SLOW_HOSTS", {}):
            audit.run(self.db, list(statuses_by_site))
        return fetched

    def test_site_that_answers_then_fails_is_stopped_and_the_rest_waits(self):
        healthy = "healthy.test"
        fetched = self.run_audit({SITE: [200] * 20 + [404] * 80, healthy: [200] * 40})
        self.assertEqual(len(fetched[SITE]), 20 + config.AUDIT_STOP_AFTER)
        self.assertEqual(len(fetched[healthy]), 40)                    # another site is not affected
        (kind, severity, message), = alerts_of(self.db)
        self.assertEqual((kind, severity), ("site_failing", "high"))
        self.assertIn(f"{config.AUDIT_STOP_AFTER} pages in a row", message)
        self.assertIn(f"({80 - config.AUDIT_STOP_AFTER} pages wait", message)
        # What was not fetched is still due; what answered 404 is due again after a day.
        self.assertEqual(len(audit.due_rows(self.db, [SITE])[SITE]), 80 - config.AUDIT_STOP_AFTER)

    def test_dead_urls_before_any_normal_answer_do_not_stop_the_audit(self):
        fetched = self.run_audit({SITE: [404] * 30 + [200] * 10})
        self.assertEqual(len(fetched[SITE]), 40)
        self.assertEqual(alerts_of(self.db), [])

    def test_scattered_failures_do_not_stop_the_audit(self):
        fetched = self.run_audit({SITE: ([200] * 3 + [404] * 5) * 6})
        self.assertEqual(len(fetched[SITE]), 48)
        self.assertNotIn("site_failing", [a[0] for a in alerts_of(self.db)])


if __name__ == "__main__":
    unittest.main()
