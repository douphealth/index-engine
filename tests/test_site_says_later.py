"""A site that answers "come back later" is left alone for the run.

On 26 Nov 2026 tenderatlas.shop's database was down and every uncached page answered 503 with Retry-After: 3600. The
audit waited 10 s on each of about 400 such pages, one at a time (over an hour, with nothing submitted for any site
meanwhile), asked the failing site twice per page, and would have recorded each page as "now HTTP 503"."""
import datetime as dt
import sqlite3
import unittest
from email.utils import format_datetime
from unittest.mock import patch

import indexengine
from engine import audit, config, store
from test_audit_transient_retry import FakeResp, Rp

SITE = "example.test"
LATER = {"Content-Type": "text/html", "Retry-After": "3600"}


def iso():
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def memory_db():
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.executescript(store.SCHEMA)
    for migration in store.MIGRATIONS:
        db.execute(migration)
    return db


def alerts_of(db):
    return [(r["kind"], r["severity"]) for r in db.execute("SELECT kind, severity FROM alerts ORDER BY id")]


def page(url, status, **extra):
    rec = {"url": url, "checked_at": iso(), "status": status, "final_url": url, "hops": 0, "ctype": "text/html", "ms": 50,
           "noindex": 0, "noindex_src": None, "canonical": None, "canonical_state": None, "title": "t", "h1": "",
           "words": 400, "sketch": "[]", "out_links": 0, "error": None, "trust": None, "links": [], "bytes": 1,
           "inline_b64": None, "description": None, "jsonld": None, "jsonld_errors": None, "robots_google": 1,
           "robots_bing": 1, "retried": None}
    rec.update(extra)
    return rec


class RetryAfter(unittest.TestCase):
    def test_reads_seconds_and_dates(self):
        self.assertEqual(audit.retry_after(FakeResp(503, headers={"Retry-After": " 120 "})), 120.0)
        when = dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=1)
        got = audit.retry_after(FakeResp(503, headers={"Retry-After": format_datetime(when, usegmt=True)}))
        self.assertTrue(3500 < got <= 3600, got)
        past = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=1)
        self.assertEqual(audit.retry_after(FakeResp(503, headers={"Retry-After": format_datetime(past, usegmt=True)})), 0.0)

    def test_missing_or_unreadable_is_none(self):
        self.assertIsNone(audit.retry_after(FakeResp(503, headers={})))
        self.assertIsNone(audit.retry_after(FakeResp(503, headers={"Retry-After": "soon"})))

    def test_only_a_long_wait_on_an_overloaded_answer_is_a_hold(self):
        body = b"<html></html>"
        self.assertEqual(audit.asks_to_come_back_later(FakeResp(503, headers=LATER), body), 3600)
        self.assertEqual(audit.asks_to_come_back_later(FakeResp(429, headers=LATER), body), 3600)
        short = {"Retry-After": str(audit.RETRY_HOLD - 1)}
        self.assertEqual(audit.asks_to_come_back_later(FakeResp(503, headers=short), body), 0)
        self.assertEqual(audit.asks_to_come_back_later(FakeResp(503, headers={}), body), 0)
        self.assertEqual(audit.asks_to_come_back_later(FakeResp(200, headers=LATER), body), 0)
        self.assertEqual(audit.asks_to_come_back_later(FakeResp(404, headers=LATER), body), 0)
        # A firewall challenge is not the site's answer about itself.
        self.assertEqual(audit.asks_to_come_back_later(FakeResp(503, headers=LATER), b"<html>Just a moment...</html>"), 0)


class FetchDoesNotWaitItOut(unittest.TestCase):
    def fetch(self, responses):
        calls = []

        def fake_get(url, **kw):
            calls.append(url)
            return responses.pop(0)
        with patch.object(audit, "get", side_effect=fake_get), patch.object(audit.time, "sleep") as sleep:
            rec = audit.fetch_page(f"https://{SITE}/a", Rp())
        return rec, calls, sleep

    def test_one_request_no_sleep_and_the_record_is_marked(self):
        rec, calls, sleep = self.fetch([FakeResp(503, headers=LATER), FakeResp(200)])
        self.assertEqual(len(calls), 1)                      # the failing site is not asked a second time
        sleep.assert_not_called()
        self.assertEqual((rec["status"], rec["hold"], rec["error"]), (503, 3600, "asked to come back later"))

    def test_a_short_retry_after_is_still_waited_and_retried(self):
        rec, calls, sleep = self.fetch([FakeResp(503, headers={"Content-Type": "text/html", "Retry-After": "5"}), FakeResp(200)])
        self.assertEqual((rec["status"], len(calls)), (200, 2))
        sleep.assert_called_once_with(5.0)
        self.assertNotIn("hold", rec)


class AuditLeavesTheSite(unittest.TestCase):
    def setUp(self):
        self.db = memory_db()
        self.addCleanup(self.db.close)

    def run_audit(self, answers):
        """answers: {site: [status or ("hold", seconds) of its 1st, 2nd, ... page]}."""
        fetched = {s: [] for s in answers}
        for site, items in answers.items():
            for i in range(len(items)):
                self.db.execute("INSERT INTO urls(url, site, in_sitemap, in_wp) VALUES (?,?,1,0)", (f"https://{site}/p{i:03d}/", site))

        def fetch_page(url, rp):
            site = url.split("/")[2]
            fetched[site].append(url)
            what = answers[site][int(url.split("/p")[1].rstrip("/"))]
            if isinstance(what, tuple):
                return page(url, 503, hold=what[1], error="asked to come back later")
            return page(url, what)
        with patch.object(audit, "fetch_page", side_effect=fetch_page), patch.object(config, "PER_HOST_CONCURRENCY", 1), \
                patch.object(config, "PER_HOST_MAX", 1), patch.object(config, "SLOW_HOSTS", {}):
            away = audit.run(self.db, list(answers))
        return away, fetched

    def test_first_such_answer_ends_the_sites_audit_and_stores_nothing_for_it(self):
        other = "healthy.test"
        away, fetched = self.run_audit({SITE: [200, 200, ("hold", 3600)] + [200] * 30, other: [200] * 25})
        self.assertEqual(away, {SITE: 3600})
        self.assertEqual(len(fetched[SITE]), 3)                                  # nothing after the answer
        self.assertEqual(len(fetched[other]), 25)                                # another site is read to the end
        stored = {r["url"]: r["status"] for r in self.db.execute("SELECT url, status FROM audits WHERE url LIKE ?", (f"https://{SITE}/%",))}
        self.assertEqual(stored, {f"https://{SITE}/p000/": 200, f"https://{SITE}/p001/": 200})
        self.assertEqual(alerts_of(self.db), [("site_unavailable", "high")])
        # Everything not read, the held page included, is still due.
        self.assertEqual(len(audit.due_rows(self.db, [SITE])[SITE]), 31)

    def test_a_page_known_as_good_keeps_its_record(self):
        url = f"https://{SITE}/p000/"
        self.db.execute("INSERT INTO audits(url, checked_at, status, title) VALUES (?,?,?,?)",
                        (url, (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=30)).isoformat(timespec="seconds"), 200, "Good"))
        away, _ = self.run_audit({SITE: [("hold", 600)]})
        self.assertEqual(away, {SITE: 600})
        row = self.db.execute("SELECT status, title FROM audits WHERE url=?", (url,)).fetchone()
        self.assertEqual((row["status"], row["title"]), (200, "Good"))
        self.assertNotIn("regression", [k for k, _ in alerts_of(self.db)])

    def test_nothing_due_returns_no_sites(self):
        self.assertEqual(audit.run(self.db, [SITE]), {})


class BreakerOnServerErrors(unittest.TestCase):
    def feed(self, statuses, **extra):
        b = audit.Breaker()
        tripped = [i for i, s in enumerate(statuses) if b.saw(dict({"status": s, "error": None}, **extra))]
        return b, tripped

    def test_server_errors_from_the_start_stop_the_site(self):
        n = config.AUDIT_STOP_AFTER
        for failing in (503, 500, 429, None):
            b, tripped = self.feed([failing] * (n + 5))
            self.assertEqual(tripped, [n - 1], failing)

    def test_dead_urls_from_the_start_still_do_not(self):
        _, tripped = self.feed([404] * 40 + [410] * 40)
        self.assertEqual(tripped, [])

    def test_a_404_between_server_errors_restarts_the_count(self):
        n = config.AUDIT_STOP_AFTER
        _, tripped = self.feed(([503] * (n - 1) + [404]) * 3)
        self.assertEqual(tripped, [])

    def test_firewall_challenges_are_not_server_errors(self):
        _, tripped = self.feed([503] * 40, error="bot challenge")
        self.assertEqual(tripped, [])


class PipelineSubmitsNothingForIt(unittest.TestCase):
    def test_held_site_is_left_out_of_submissions_and_sitemap_announcements(self):
        import argparse
        sites = [SITE, "healthy.test"]
        args = argparse.Namespace(skip_discover=True, skip_audit=False, force_audit=False, audit_max=None, inspect_max=0,
                                  no_submit=False, dry_run=False, both=False, include_taxonomy=False, submit_max=None,
                                  no_creds=True, no_links=True, no_wp=True, site=None)
        seen = {}

        class Stop(Exception):
            pass

        def ping(db, up, dry_run=False):
            seen["ping"] = list(up)
            raise Stop                                   # the rest of the pipeline is not under test

        with patch.object(indexengine.audit, "run", return_value={SITE: 3600}), \
                patch.object(indexengine, "engines", return_value=(object(), object(), {s: "sc-domain:" + s for s in sites},
                                                                    {s: "https://" + s + "/" for s in sites})), \
                patch.object(indexengine.traffic, "pull"), patch.object(indexengine.google, "pull_performance"), \
                patch.object(indexengine.queries, "pull_google"), patch.object(indexengine.measured, "measure"), \
                patch.object(indexengine.bing, "pull_performance"), patch.object(indexengine.bing, "pull_inbound_links"), \
                patch.object(indexengine.queries, "pull_bing"), \
                patch.object(indexengine.submit, "run", side_effect=lambda db, up, **kw: seen.update(submit=list(up)) or {}), \
                patch.object(indexengine.google, "sync_sitemaps", side_effect=lambda db, c, props, **kw: seen.update(google=sorted(props))), \
                patch.object(indexengine.bing, "sync_feeds", side_effect=lambda db, c, bs, **kw: seen.update(bing=sorted(bs))), \
                patch.object(indexengine.sitemaps, "ping_hubs", side_effect=ping):
            with self.assertRaises(Stop):
                indexengine.pipeline(None, args, sites)
        self.assertEqual(seen, {"submit": ["healthy.test"], "google": ["healthy.test"], "bing": ["healthy.test"],
                                "ping": ["healthy.test"]})


if __name__ == "__main__":
    unittest.main()
