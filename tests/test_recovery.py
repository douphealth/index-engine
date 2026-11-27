"""Recovery: collapse detection, the prune plan's decisions and the scaled-content (template) scan."""
import datetime as dt
import sqlite3
import unittest
import zlib

from engine import recovery as rc, store


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


def series(levels, end="2026-11-16"):
    """Daily (day, impressions, clicks) from a list of (days, impressions/day), oldest first."""
    total = sum(n for n, _ in levels)
    day = dt.date.fromisoformat(end) - dt.timedelta(days=total - 1)
    out = []
    for n, v in levels:
        for _ in range(n):
            out.append((day.isoformat(), v, v // 50))
            day += dt.timedelta(days=1)
    return out


S = "site.example"
U = lambda p: f"https://{S}/{p}/"  # noqa: E731


def add_page(db, path, *, gc=0, gi=0, bc=0, bi=0, words=1200, title=None, kind="post", age=400):
    url = U(path) if path else f"https://{S}/"
    first = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=age)).isoformat()
    db.execute("INSERT INTO urls(url, site, kind, first_seen) VALUES(?,?,?,?)", (url, S, kind, first))
    db.execute("INSERT INTO audits(url, title, words) VALUES(?,?,?)", (url, title or path.replace("-", " ").title(), words))
    db.execute("INSERT INTO perf(url, site, gsc_clicks, gsc_impr, gsc_at, bing_clicks, bing_impr, bing_at) VALUES(?,?,?,?,?,?,?,?)",
               (url, S, gc, gi, "x", bc, bi, "x"))
    return url


class VisibilityTests(unittest.TestCase):
    def test_collapse_months_ago_is_not_read_as_stable(self):
        # 300 days at 400/day, then 100 days at 5/day: the 28-vs-28 test sees "stable", this must see "collapsed"
        v = rc.engine_visibility(series([(300, 400), (100, 5)]))
        self.assertEqual(v["state"], "collapsed")
        self.assertLess(v["ratio"], 0.05)
        self.assertEqual(v["step"]["day"], series([(300, 400), (100, 5)])[300][0])

    def test_steady_site_is_stable_and_has_no_step(self):
        v = rc.engine_visibility(series([(400, 200)]))
        self.assertEqual(v["state"], "stable")
        self.assertIsNone(v["step"])

    def test_a_small_site_is_not_called_collapsed(self):
        v = rc.engine_visibility(series([(300, 6), (100, 1)]))
        self.assertEqual(v["state"], "never_visible")

    def test_severe_drop_and_growth(self):
        self.assertEqual(rc.engine_visibility(series([(300, 100), (100, 40)]))["state"], "severe_drop")
        self.assertEqual(rc.engine_visibility(series([(300, 100), (100, 200)]))["state"], "growing")

    def test_short_history_is_not_judged(self):
        self.assertEqual(rc.engine_visibility(series([(40, 100)]))["state"], "too_little_data")

    def test_site_visibility_gives_manual_action_link_first(self):
        db = new_db()
        for day, i, c in series([(300, 400), (100, 3)]):
            db.execute("INSERT INTO traffic_daily(site, day, src, clicks, impr) VALUES(?,?,?,?,?)", (S, day, "google", c, i))
        v = rc.visibility(db, S, {"gsc_property": "sc-domain:site.example"})
        self.assertEqual((v["state"], v["engine"]), ("collapsed", "google"))
        self.assertIn("manual-actions?resource_id=sc-domain%3Asite.example", v["steps"][0]["href"])
        self.assertIn("of normal", v["headline"])
        self.assertTrue(rc.needs_recovery(v, []))


class PrunePlanTests(unittest.TestCase):
    def plan(self, db, buckets, dups=None, inbound=None, artifacts=None):
        return {x["url"]: x for x in rc.prune_plan(db, S, buckets, dups or {}, inbound, artifacts)}

    def test_decisions(self):
        db = new_db()
        home = add_page(db, "", gi=500, gc=20, kind="page")
        earner = add_page(db, "best-shoes", gc=12, gi=900)
        flagged_earner = add_page(db, "trail-guide", bc=3, bi=400)
        dead_thin = add_page(db, "old-thin-post", words=180)
        dead_unindexed = add_page(db, "forgotten-post")
        dead_linked = add_page(db, "linked-shoe-post", title="Trail Running Shoe Cushion Guide")
        target = add_page(db, "trail-running-shoe-cushion-review", gc=5, gi=300, title="Trail Running Shoe Cushion Review")
        weak_dup = add_page(db, "copy-of-best-shoes")
        young = add_page(db, "brand-new-post", age=20)
        quiet_indexed = add_page(db, "quiet-but-indexed")
        demand_flagged = add_page(db, "some-demand", gi=40)
        b = {home: ("INDEXED", []), earner: ("INDEXED", []), flagged_earner: ("INDEXED", ["TRUST_RISK"]),
             dead_thin: ("INDEXED", ["THIN"]), dead_unindexed: ("CRAWLED_NOT_INDEXED", []),
             dead_linked: ("CRAWLED_NOT_INDEXED", []), target: ("INDEXED", []), weak_dup: ("INDEXED", ["NEAR_DUPLICATE"]),
             young: ("UNKNOWN_TO_GOOGLE", []), quiet_indexed: ("INDEXED", []), demand_flagged: ("INDEXED", ["THIN"])}
        p = self.plan(db, b, dups={weak_dup: (earner, 0.8)}, inbound={dead_linked: 4})
        got = {u: p[u]["decision"] for u in p}
        self.assertEqual(got[home], "keep")
        self.assertEqual(got[earner], "keep")
        self.assertEqual(got[flagged_earner], "fix")
        self.assertEqual(got[dead_thin], "noindex")
        self.assertEqual(got[dead_unindexed], "noindex")
        self.assertEqual(got[dead_linked], "redirect")
        self.assertEqual(p[dead_linked]["target"], target)
        self.assertEqual(got[weak_dup], "merge")
        self.assertEqual(p[weak_dup]["target"], earner)
        self.assertEqual(got[young], "review")
        self.assertEqual(got[quiet_indexed], "review")
        self.assertEqual(got[demand_flagged], "fix")

    def test_blocked_and_ignored_pages_are_left_out(self):
        db = new_db()
        a = add_page(db, "noindexed")
        b = add_page(db, "ignored")
        p = self.plan(db, {a: ("NOINDEX", []), b: ("CRAWLED_NOT_INDEXED", ["IGNORED"])})
        self.assertEqual(p, {})

    def test_template_artifacts_count_as_a_quality_flag(self):
        db = new_db()
        a = add_page(db, "templated-post")
        p = self.plan(db, {a: ("INDEXED", [])}, artifacts={a: ["7 headings follow one numbered template"]})
        self.assertEqual(p[a]["decision"], "noindex")
        self.assertIn("TEMPLATE_ARTIFACT", p[a]["flags"])
        self.assertEqual(p[a]["artifacts"], ["7 headings follow one numbered template"])

    def test_noindex_needs_both_engines_quiet(self):
        db = new_db()
        a = add_page(db, "bing-only-earner", bc=2, bi=60)
        p = self.plan(db, {a: ("CRAWLED_NOT_INDEXED", ["THIN"])})
        self.assertEqual(p[a]["decision"], "fix")


class TemplateScanTests(unittest.TestCase):
    def corpus(self, db, url, raw):
        db.execute("INSERT INTO link_corpus(url, site, post_id, post_type, modified, raw) VALUES(?,?,?,?,?,?)",
                   (url, S, 1, "posts", "x", zlib.compress(raw.encode())))

    def test_finds_numbered_headings_leftovers_and_boilerplate(self):
        db = new_db()
        boiler = "<h2>Direct Answer for Searchers &amp; AI Engines</h2><h2>Editorial promise</h2>"
        self.corpus(db, U("nimbus"), boiler + "".join(f"<h2>Use-case notes for Nimbus review #{k}</h2><p>Template text: a light trainer that held "
                                                       f"up well on easy runs and long days.</p>" for k in range(1, 8)))
        self.corpus(db, U("birthday"), "".join(f"<h2>The Meaning of Birthday Number {k}</h2><p>Number {k} brings "
                                               f"{'its own ' * k}distinct trait {k * 7} described here.</p>" for k in range(1, 8)))
        self.corpus(db, U("trail"), boiler + "<h2>🖼️ Visual Examples From the WordPress Media Library</h2>")
        for k in range(6):
            self.corpus(db, U(f"post-{k}"), boiler + f"<h2>Unique heading {k}a</h2><p>Text.</p>")
        self.corpus(db, U("clean"), "<h2>Why cushioning matters</h2><p>Plain text.</p>")
        scan = rc.template_scan(db, S)
        self.assertEqual(scan["scanned"], 10)
        self.assertTrue(any("numbered template" in f for f in scan["pages"][U("nimbus")]))
        self.assertNotIn(U("birthday"), scan["pages"], "headings numbered by their subject are not a template")
        self.assertTrue(any("leftover" in f for f in scan["pages"][U("trail")]))
        self.assertTrue(any("repeated across the site" in f for f in scan["pages"][U("post-0")]))
        self.assertNotIn(U("clean"), scan["pages"])
        self.assertEqual(scan["boilerplate"][0][1], 8)
        self.assertEqual(rc.template_message(scan)[0], "site")

    def test_prompt_placeholders_are_not_leftovers(self):
        db = new_db()
        self.corpus(db, U("prompt"), "<h2>The prompt</h2><p>Write about {{topic}} and replace the placeholder with your niche.</p>")
        self.assertEqual(rc.template_scan(db, S)["pages"], {})

    def test_training_plans_and_common_sections_are_not_templates(self):
        db = new_db()
        for k in range(8):
            plan = "".join(f"<h2>Week {w}</h2>" for w in range(1, 9))
            self.corpus(db, U(f"plan-{k}"), plan + "<h2>Frequently Asked Questions</h2><h2>Conclusion</h2><h2>Key Takeaways</h2>"
                        + f"<h2>Topic {k} detail</h2>")
        scan = rc.template_scan(db, S)
        self.assertEqual(scan["pages"], {})
        self.assertEqual(scan["boilerplate"], [])

    def test_a_few_years_in_headings_are_not_a_template(self):
        db = new_db()
        self.corpus(db, U("years"), "<h2>Best shoes 2026</h2><h2>Shoes 2025 vs 2026</h2><h3>Top 3 picks</h3>")
        self.assertEqual(rc.template_scan(db, S)["pages"], {})


if __name__ == "__main__":
    unittest.main()
