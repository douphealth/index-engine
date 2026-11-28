"""Prune queue (eligibility, the helper, apply/verify/undo against a fake WordPress), crawler telemetry, and the
randomized holdout."""
import datetime as dt
import ipaddress
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from engine import config, crawl, holdout, prune, results, store

S = "site.example"
U = f"https://{S}/old-post/"


def new_db():
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.executescript(store.SCHEMA)
    for sql in store.MIGRATIONS:
        try:
            db.execute(sql)
        except sqlite3.OperationalError:
            pass
    return db


def plan_row(**kw):
    row = {"url": U, "decision": "noindex", "google_clicks_90d": 0, "bing_clicks": 0, "google_impr_90d": 3, "bing_impr": 2,
           "age_days": 400, "inbound_links": 0, "why": "No search value.", "title": "Old post"}
    row.update(kw)
    return row


class EligibleTest(unittest.TestCase):
    def test_clear_cut_noindex(self):
        self.assertEqual(prune.eligible(plan_row())[0], "noindex")

    def test_anything_doubtful_is_left_to_the_recovery_page(self):
        for kw in ({"bing_clicks": 1}, {"google_impr_90d": 25}, {"age_days": 90}, {"age_days": None},
                   {"inbound_links": None}, {"inbound_links": 2}, {"decision": "review"}, {"decision": "merge"}):
            self.assertIsNone(prune.eligible(plan_row(**kw)), kw)

    def test_a_page_the_engine_linked_to_is_not_pruned(self):
        self.assertIsNone(prune.eligible(plan_row(), linked={U}))

    def test_redirect_needs_a_target(self):
        self.assertIsNone(prune.eligible(plan_row(decision="redirect", inbound_links=3)))
        got = prune.eligible(plan_row(decision="redirect", inbound_links=3, target=f"https://{S}/better/"))
        self.assertEqual(got[0], "redirect")

    def test_excluded_sites_get_no_proposals(self):
        db = new_db()
        res = {"gearuptofit.com": {"recovery": {"plan": [plan_row(url="https://gearuptofit.com/x/")]}},
               S: {"recovery": {"plan": [plan_row()]}}}
        out = prune.propose(db, res, ["gearuptofit.com", S])
        self.assertEqual([p["site"] for p in out], [S])
        self.assertEqual(out[0]["id"], prune.prune_id(U))


class Resp:
    def __init__(self, status=200, text="", headers=None, js=None):
        self.status_code, self.text, self.headers, self._js = status, text, headers or {}, js

    def json(self):
        return self._js

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class FakeWP:
    """Code Snippets + the helper's REST route, in memory."""
    def __init__(self, helper=True, breaks_home=False):
        self.site, self.base, self.ok = S, f"https://{S}", True
        self.helper, self.breaks_home = helper, breaks_home
        self.rules = {"noindex": {}, "redirects": {}}
        self.snippets, self.calls = [], []
        self.s = self

    def request(self, method, url, timeout=None, json=None):
        path = url.split("/wp-json/", 1)[1]
        self.calls.append((method, path))
        if path == prune.ROUTE:
            if not self.helper:
                return Resp(404, js={})
            if method == "POST":
                for i in json.get("add_noindex", []):
                    self.rules["noindex"][str(i)] = "t"
                for i in json.get("remove_noindex", []):
                    self.rules["noindex"].pop(str(i), None)
                for p, x in (json.get("add_redirects") or {}).items():
                    self.rules["redirects"][p] = x
                for p in json.get("remove_redirects", []):
                    self.rules["redirects"].pop(p, None)
            return Resp(200, js={"version": 1, "rules": self.rules})
        if path == "code-snippets/v1/snippets" and method == "GET":
            return Resp(200, js=self.snippets)
        if path == "code-snippets/v1/snippets" and method == "POST":
            self.snippets.append({"id": 7, "name": json["name"], "active": True})
            self.helper = True
            return Resp(201, js={"id": 7, "active": True})
        if path.endswith("/deactivate"):
            self.helper = False
            return Resp(200, js={})
        return Resp(404, js={})


class HelperTest(unittest.TestCase):
    def test_installs_once_and_checks_the_home_page(self):
        wpc = FakeWP(helper=False)
        with patch.object(prune, "_home_ok", return_value=True), patch.object(prune.time, "sleep"):
            ok, msg = prune.install_helper(wpc)
        self.assertTrue(ok, msg)
        self.assertEqual(len(wpc.snippets), 1)
        self.assertEqual(prune.install_helper(wpc), (True, "helper already active"))

    def test_switched_off_again_when_the_home_page_breaks(self):
        wpc = FakeWP(helper=False)
        with patch.object(prune, "_home_ok", return_value=False), patch.object(prune.time, "sleep"):
            ok, msg = prune.install_helper(wpc)
        self.assertFalse(ok)
        self.assertIn("switched off again", msg)
        self.assertFalse(wpc.helper)

    def test_never_installed_on_an_excluded_site(self):
        wpc = FakeWP(helper=False)
        wpc.site = "gearuptofit.com"
        ok, _msg = prune.install_helper(wpc)
        self.assertFalse(ok)
        self.assertEqual(wpc.calls, [])

    def test_helper_code_is_plain_php_without_an_open_tag(self):
        code = prune.HELPER_CODE
        self.assertNotIn("<?php", code)
        self.assertEqual(code.count("{"), code.count("}"))
        self.assertEqual(code.count("("), code.count(")"))
        self.assertNotIn("fn(", code)                      # arrow functions need PHP 7.4: keep it to PHP 7.0


class ServedTest(unittest.TestCase):
    def test_meta_and_header(self):
        self.assertTrue(prune.served_noindex(Resp(text='<meta name="robots" content="noindex, follow">')))
        self.assertTrue(prune.served_noindex(Resp(text="<p>x</p>", headers={"X-Robots-Tag": "noindex, follow"})))
        self.assertFalse(prune.served_noindex(Resp(text='<meta name="robots" content="index, follow">')))
        self.assertFalse(prune.served_noindex(Resp(text='<meta name="description" content="noindex is a word">')))


class ApplyTest(unittest.TestCase):
    def setUp(self):
        self.db = new_db()
        self.db.execute("INSERT INTO link_corpus(url, site, post_id, post_type) VALUES(?,?,?,?)", (U, S, 42, "posts"))
        self.reports = []
        self.report = lambda *a: self.reports.append(a)

    def run_apply(self, items, wpc, live=(True, True)):
        with patch.object(prune, "live_state", return_value=live), patch("engine.links.purge", return_value="purged"), \
                patch("engine.links._log_change"):
            return prune.apply(self.db, items, {}, {S: wpc}, self.report)

    def test_only_approved_rows_are_applied(self):
        wpc = FakeWP()
        self.run_apply([{"id": "a" * 16, "site": S, "url": U, "action": "noindex", "status": "proposed"}], wpc)
        self.assertEqual(wpc.rules["noindex"], {})
        self.assertEqual(self.reports, [])

    def test_noindex_verified_then_undone(self):
        wpc = FakeWP()
        it = {"id": "a" * 16, "site": S, "url": U, "action": "noindex", "status": "approved"}
        self.assertEqual(self.run_apply([it], wpc), {S})
        self.assertIn("42", wpc.rules["noindex"])
        self.assertEqual(self.reports[-1][1], "applied")
        self.assertIn("verified on the live page", self.reports[-1][2])
        self.run_apply([dict(it, status="undo")], wpc)
        self.assertEqual(wpc.rules["noindex"], {})
        self.assertEqual(self.reports[-1][1], "undone")

    def test_a_rule_the_site_does_not_serve_is_taken_back(self):
        wpc = FakeWP()
        it = {"id": "a" * 16, "site": S, "url": U, "action": "noindex", "status": "approved"}
        self.run_apply([it], wpc, live=(False, False))
        self.assertEqual(wpc.rules["noindex"], {})
        self.assertEqual(self.reports[-1][1], "failed")

    def test_redirect_rule_keyed_by_path(self):
        wpc = FakeWP()
        it = {"id": "b" * 16, "site": S, "url": U, "action": "redirect", "target": f"https://{S}/better/", "status": "approved"}
        self.run_apply([it], wpc)
        self.assertEqual(wpc.rules["redirects"]["/old-post/"]["to"], f"https://{S}/better/")

    def test_daily_cap(self):
        wpc = FakeWP()
        items = [{"id": f"{i:016x}", "site": S, "url": U, "action": "noindex", "status": "approved"} for i in range(3)]
        with patch.object(prune, "live_state", return_value=(True, True)), patch("engine.links.purge", return_value="x"), \
                patch("engine.links._log_change"):
            prune.apply(self.db, items, {"prunes_per_site_per_day": 2}, {S: wpc}, self.report)
        self.assertEqual(sum(1 for r in self.reports if r[1] == "applied"), 2)


class CrawlTest(unittest.TestCase):
    def test_spans_are_consecutive_and_capped(self):
        days = [f"2026-11-{d:02d}" for d in (1, 2, 3, 5, 6, 7, 8, 9, 10, 11, 12, 13)]
        got = crawl.spans(days, size=7)
        self.assertEqual(got[0], ["2026-11-01", "2026-11-02", "2026-11-03"])
        self.assertEqual(len(got[1]), 7)
        self.assertEqual(got[2], ["2026-11-12", "2026-11-13"])

    def test_due_days_skip_what_was_read(self):
        db = new_db()
        db.executescript(crawl.SCHEMA)
        today = dt.date(2026, 11, 28)
        db.execute("INSERT INTO crawl_days(site, day, read_at, rows, truncated, verified) VALUES(?,?,?,?,?,1)", (S, "2026-11-27", "t", 0, 0))
        got = crawl.due_days(db, S, today, keep=3)
        self.assertEqual(got, ["2026-11-25", "2026-11-26"])

    def test_zone_is_the_longest_suffix(self):
        zm = {"example": ("t1", "z1"), "site.example": ("t2", "z2")}
        self.assertEqual(crawl.zone_for(S, zm), ("t2", "z2"))
        self.assertIsNone(crawl.zone_for("other.test", zm))

    def seeded(self):
        db = new_db()
        db.executescript(crawl.SCHEMA)
        db.execute("INSERT INTO urls(url, site) VALUES(?,?)", (U, S))
        rows = [(S, "2026-11-26", "google", "/old-post/", 200, 3), (S, "2026-11-27", "google", "/old-post", 200, 1),
                (S, "2026-11-27", "google", "/gone/", 404, 150), (S, "2026-11-27", "google", "/x/", 200, 50),
                (S, "2026-11-27", "bing", "/old-post/", 200, 2)]
        db.executemany("INSERT INTO crawl_hits VALUES(?,?,?,?,?,?)", rows)
        for d in ("2026-11-26", "2026-11-27"):
            db.execute("INSERT INTO crawl_days(site, day, read_at, rows, truncated, verified) VALUES(?,?,?,?,?,1)", (S, d, "t", 1, 0))
        return db

    def test_pages_match_with_and_without_slash(self):
        got = crawl.pages(self.seeded(), S)[U]
        self.assertEqual((got["g_30d"], got["g_last"], got["b_last"]), (4, "2026-11-27", "2026-11-27"))

    def test_after_changes_sees_the_refetch(self):
        db = self.seeded()
        db.execute("INSERT INTO link_ops(id, site, source, target, status, applied_at) VALUES(?,?,?,?,?,?)",
                   ("l1", S, f"https://{S}/src/", U, "applied", "2026-11-26T10:00:00+00:00"))
        with patch.object(crawl, "dt") as fake:
            fake.datetime.now.return_value = dt.datetime(2026, 11, 28, tzinfo=dt.timezone.utc)
            fake.timedelta, fake.date, fake.timezone = dt.timedelta, dt.date, dt.timezone
            got = crawl.after_changes(db)
        self.assertEqual(got["kinds"]["link"], {"n": 1, "refetched": 1, "pending": 0})

    def test_missing_pages_alert(self):
        db = self.seeded()
        crawl.raise_alerts(db, [S])
        kinds = [r["kind"] for r in db.execute("SELECT kind FROM alerts")]
        self.assertIn("googlebot_missing", kinds)
        self.assertNotIn("googlebot_errors", kinds)


class VerifiedCrawlerTest(unittest.TestCase):
    """Only requests from the engines' published crawler ranges count (28 Nov 2026: 52% of gearuptofit's "Googlebot"
    requests in a week came from the owner's PC or from other networks)."""
    NETS = {"google": [ipaddress.ip_network("66.249.64.0/27"), ipaddress.ip_network("2001:4860:4801:10::/64")],
            "bing": [ipaddress.ip_network("157.55.39.0/24")]}

    def test_verifier_checks_the_right_engine_and_both_ip_versions(self):
        v = crawl.Verifier(self.NETS)
        self.assertTrue(v.is_real("google", "66.249.64.5"))
        self.assertTrue(v.is_real("google", "2001:4860:4801:10::1"))
        self.assertFalse(v.is_real("google", "94.65.157.157"))      # a PC sending a Googlebot user agent
        self.assertFalse(v.is_real("bing", "66.249.64.5"))          # Google's address is not Bingbot
        self.assertFalse(v.is_real("google", "not an ip"))

    def test_read_span_counts_impostors_apart(self):
        rows = [{"count": 3, "dimensions": {"date": "2026-11-27", "clientRequestPath": "/a/", "edgeResponseStatus": 200, "clientIP": "66.249.64.9"}},
                {"count": 7, "dimensions": {"date": "2026-11-27", "clientRequestPath": "/a/", "edgeResponseStatus": 200, "clientIP": "94.65.157.157"}},
                {"count": 2, "dimensions": {"date": "2026-11-27", "clientRequestPath": "/b/", "edgeResponseStatus": 404, "clientIP": "66.249.64.9"}}]

        class Session:
            def post(self, *a, **k):
                return Resp(200, js={"data": {"viewer": {"zones": [{"google": rows, "bing": []}]}}})
        hits, fake, truncated = crawl.read_span("t", "z", S, ["2026-11-27"], Session(), crawl.Verifier(self.NETS))
        self.assertEqual(hits["2026-11-27"], {("google", "/a/", 200): 3, ("google", "/b/", 404): 2})
        self.assertEqual(fake["2026-11-27"], {"google": 7})
        self.assertFalse(truncated)

    def test_ranges_are_cached_refreshed_and_kept_when_a_download_fails(self):
        lists = {u: {"prefixes": [{"ipv4Prefix": "66.249.64.0/27"}] if "google" in u else [{"ipv4Prefix": "157.55.39.0/24"}]}
                 for urls in crawl.VERIFY_LISTS.values() for u in urls}

        class Session:
            def __init__(self, fail=False):
                self.fail, self.calls = fail, 0

            def get(self, url, timeout=None):
                self.calls += 1
                if self.fail:
                    raise crawl.requests.ConnectionError("offline")
                return Resp(200, js=lists[url])
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "ranges.json"
            t0 = dt.datetime(2026, 11, 28, tzinfo=dt.timezone.utc)
            with patch.object(crawl, "log"):
                self.assertIsNone(crawl.ranges(Session(fail=True), path, t0))        # never downloaded: nothing
                s1 = Session()
                got = crawl.ranges(s1, path, t0)
                self.assertEqual(got["google"], [ipaddress.ip_network("66.249.64.0/27")])
                s2 = Session()
                crawl.ranges(s2, path, t0 + dt.timedelta(days=3))                    # fresh: no download
                self.assertEqual(s2.calls, 0)
                kept = crawl.ranges(Session(fail=True), path, t0 + dt.timedelta(days=9))  # stale and offline: last copy
                self.assertEqual(kept["bing"], [ipaddress.ip_network("157.55.39.0/24")])

    def test_data_read_before_verification_is_read_again(self):
        db = new_db()
        db.execute("CREATE TABLE crawl_days(site TEXT, day TEXT, read_at TEXT, rows INT, truncated INT DEFAULT 0, PRIMARY KEY(site, day))")
        db.execute("CREATE TABLE crawl_hits(site TEXT, day TEXT, bot TEXT, path TEXT, status INT, hits INT, PRIMARY KEY(site, day, bot, path, status))")
        db.execute("INSERT INTO crawl_days VALUES(?,?,?,?,?)", (S, "2026-11-27", "t", 1, 0))
        db.execute("INSERT INTO crawl_hits VALUES(?,?,?,?,?,?)", (S, "2026-11-27", "google", "/a/", 200, 9))
        crawl._migrate(db)
        self.assertEqual(db.execute("SELECT COUNT(*) FROM crawl_hits").fetchone()[0], 0)
        self.assertEqual(crawl.due_days(db, S, dt.date(2026, 11, 28), keep=1), ["2026-11-27"])

    def test_missing_addresses_are_sorted_by_kind_and_moved_pages_named(self):
        self.assertEqual(crawl.missing_kind("/wp-content/plugins/phastpress/phast.php/c2Vy"), "plugin or theme file")
        self.assertEqual(crawl.missing_kind("/wp-content/uploads/2020/03/shoe.jpg"), "image or file")
        self.assertEqual(crawl.missing_kind("/ads.txt"), "image or file")
        self.assertEqual(crawl.missing_kind("/category/running/page/108/"), "archive page past the end")
        self.assertEqual(crawl.missing_kind("/old-post/"), "page")
        db = new_db()
        db.execute("INSERT INTO urls(url, site) VALUES(?,?)", (f"https://{S}/fitness/walking-vs-running/", S))
        self.assertEqual(crawl.moved_to(db, S, "/walking-vs-running/"), f"https://{S}/fitness/walking-vs-running/")
        self.assertIsNone(crawl.moved_to(db, S, "/deleted-post/"))
        self.assertIsNone(crawl.moved_to(db, S, "/fitness/walking-vs-running/"))   # the live address itself


class HoldoutTest(unittest.TestCase):
    def test_arm_is_fixed_and_about_the_share_asked(self):
        urls = [f"https://{S}/p{i}/" for i in range(2000)]
        arms = [holdout.arm("link", S, u, 10) for u in urls]
        self.assertEqual(arms, [holdout.arm("link", S, u, 10) for u in urls])
        self.assertTrue(150 < arms.count("hold") < 250, arms.count("hold"))

    def test_held_for_31_days_then_released(self):
        db = new_db()
        urls = [f"https://{S}/p{i}/" for i in range(200)]
        t0 = dt.datetime(2026, 11, 1, tzinfo=dt.timezone.utc)
        held = holdout.assign(db, "link", S, urls, pct=10, now=t0)
        self.assertEqual(held, {u for u in urls if holdout.arm("link", S, u, 10) == "hold"})
        self.assertEqual(holdout.assign(db, "link", S, urls, pct=10, now=t0 + dt.timedelta(days=30)), held)
        self.assertEqual(holdout.assign(db, "link", S, urls, pct=10, now=t0 + dt.timedelta(days=31)), set())

    def test_off_enrols_nothing(self):
        db = new_db()
        self.assertEqual(holdout.assign(db, "link", S, [U], pct=0), set())
        self.assertEqual(db.execute("SELECT COUNT(*) FROM experiment").fetchone()[0], 0)

    def test_summary_compares_the_arms(self):
        db = new_db()
        t0 = dt.datetime(2026, 9, 1, tzinfo=dt.timezone.utc)
        for i in range(40):
            u, a = f"https://{S}/p{i}/", "hold" if i < 20 else "treat"
            db.execute("INSERT INTO experiment VALUES(?,?,?,?,?,?)", ("link", S, u, a, t0.isoformat(), "crawled"))
            if a == "treat" and i % 2 == 0 or a == "hold" and i < 4:
                db.execute("INSERT INTO gsc_history(url, inspected_at, coverage) VALUES(?,?,?)",
                           (u, (t0 + dt.timedelta(days=10)).isoformat(), "Submitted and indexed"))
        got = holdout.summary(db, now=dt.datetime(2026, 11, 28, tzinfo=dt.timezone.utc))["link"]
        self.assertEqual((got["treat"]["n"], got["treat"]["indexed"], got["hold"]["indexed"]), (20, 10, 4))
        self.assertAlmostEqual(got["lift"], 0.3)
        self.assertTrue(got["ready"])
        self.assertLess(got["p_value"], 0.06)

    def test_randomized_result_outranks_the_observational_one(self):
        obs = {"growth": None, "index": {"n": 40, "lift": -0.1}}
        exp = {"link": {"ready": True, "lift": 0.3, "treat": {"n": 200}, "hold": {"n": 25}}}
        got = results.calibration(None, obs, exp=exp)
        self.assertEqual(got["source"]["index"], "randomized")
        self.assertEqual(got["index"], 1.5)

    def test_setting_is_respected(self):
        with patch.object(config, "HOLDOUT_PCT", 0):
            self.assertEqual(holdout.assign(new_db(), "cleanup", S, [U]), set())


if __name__ == "__main__":
    unittest.main()
