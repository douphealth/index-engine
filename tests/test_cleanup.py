"""Cleanup queue: which sections are cut, which are kept, and how apply/verify/undo behave against a fake WordPress."""
import json
import sqlite3
import tempfile
import unittest
import zlib
from pathlib import Path
from unittest.mock import patch

from engine import cleanup as cl, config, store

S = "site.example"


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


def para(n, seed="word"):
    return "<p>" + " ".join(f"{seed}{k}" for k in range(n)) + ".</p>"


# Scaffolding repeats one text under each numbered heading (on the live sites the texts overlap 0.98-1.00).
NUMBERED = "".join(f"<h2>Use-case notes for ASICS Gel-Nimbus 27 review #{k}</h2>{para(30, 'note')}" for k in range(1, 8))
# Content numbered by its subject has its own text under each heading (mysticaldigits.com, 27 Nov 2026).
SUBJECT_NUMBERED = "".join(f"<h2>The Meaning of Birthday Number {k}</h2>{para(60, f'meaning{k}x')}" for k in range(1, 8))
BOILER = ("<h2>Editorial promise</h2><p>We publish only what we can check. Every claim links to its source, every update is dated, "
          "and corrections are made in the open within two working days of a report.</p>")


def post(body, unique="u"):
    return f"<h2>What the shoe does well</h2>{para(80, unique)}" + body + f"<h2>Who should buy it</h2>{para(40, unique + 'w')}"


class SectionTests(unittest.TestCase):
    def test_sections_run_to_the_next_heading_of_the_same_or_higher_level(self):
        raw = "<h2>A</h2><p>a</p><h3>A1</h3><p>a1</p><h2>B</h2><p>b</p>"
        secs = {s["heading"]: s for s in cl.sections(raw)}
        self.assertEqual(raw[secs["A"]["start"]:secs["A"]["end"]], "<h2>A</h2><p>a</p><h3>A1</h3><p>a1</p>")
        self.assertTrue(secs["A"]["bounded"])
        self.assertFalse(secs["B"]["bounded"])

    def test_gutenberg_comments_go_with_the_heading(self):
        raw = ('<!-- wp:heading -->\n<h2 class="wp-block-heading">A</h2>\n<!-- /wp:heading -->\n\n<!-- wp:paragraph -->\n<p>a</p>\n'
               '<!-- /wp:paragraph -->\n\n<!-- wp:heading -->\n<h2>B</h2>\n<!-- /wp:heading -->')
        a = cl.sections(raw)[0]
        cut = raw[a["start"]:a["end"]]
        self.assertTrue(cut.startswith("<!-- wp:heading -->") and cut.rstrip().endswith("<!-- /wp:paragraph -->"))
        self.assertTrue(cl.balanced(cut))

    def test_a_cut_that_leaves_a_block_open_is_refused(self):
        self.assertFalse(cl.balanced('<h2>X</h2><div class="box"><p>x</p>'))
        self.assertFalse(cl.balanced("<!-- wp:group --><div><h2>X</h2></div>"))
        self.assertTrue(cl.balanced("<h2>X</h2><p>open paragraph<ul><li>a<li>b</ul>"))


class FindCutsTests(unittest.TestCase):
    def test_numbered_template_and_leftover_sections_are_cut(self):
        raw = post(NUMBERED + "<h2>🖼️ Visual Examples From the WordPress Media Library</h2>" + para(20))
        cuts = cl.find_cuts(raw)
        kinds = [c["kind"] for c in cuts]
        self.assertEqual(kinds.count("numbered"), 7)
        self.assertEqual(kinds.count("leftover"), 1)
        self.assertNotIn("What the shoe does well", [c["heading"] for c in cuts])

    def test_headings_numbered_by_their_subject_are_kept(self):
        self.assertEqual(cl.find_cuts(post(SUBJECT_NUMBERED)), [])
        faq = "".join(f"<h3>What are the {k} C’s of content marketing?</h3>{para(70, f'c{k}x')}" for k in (3, 4, 5))
        self.assertEqual(cl.find_cuts(post(faq)), [])

    def test_a_numbered_run_with_mostly_shared_text_is_still_cut(self):
        mostly = "".join(f"<h2>Example {k}: adjust slowly and observe clearly</h2>{para(40, 'same')}<p>Pot {k}.</p>"
                         for k in range(1, 9))
        self.assertEqual([c["kind"] for c in cl.find_cuts(post(mostly))], ["numbered"] * 8)

    def test_boilerplate_needs_near_identical_text_on_five_posts(self):
        posts = [post(BOILER, f"p{k}") for k in range(6)]
        unique_answer = [post(f"<h2>Direct answer for searchers</h2>{para(40, f'ans{k}')}", f"q{k}") for k in range(6)]
        idx = cl.boilerplate_index(posts + unique_answer)
        self.assertEqual([c["kind"] for c in cl.find_cuts(posts[0], idx)], ["boilerplate"])
        self.assertEqual(cl.find_cuts(unique_answer[0], idx), [])          # same heading, its own content: kept
        self.assertEqual(cl.find_cuts(post(BOILER, "x"), cl.boilerplate_index(posts[:3])), [])   # only on 3 posts

    def test_training_plans_and_long_or_unbounded_sections_are_left_alone(self):
        plan = "".join(f"<h2>Week {k}</h2>{para(30)}" for k in range(1, 9))
        self.assertEqual(cl.find_cuts(post(plan)), [])
        long_tail = "<h2>Visual Examples From the WordPress Media Library</h2>" + para(400)      # last section, 400 words
        self.assertEqual(cl.find_cuts(f"<h2>Intro</h2>{para(20)}" + long_tail), [])

    def test_cut_removes_exactly_the_sections_and_refinds_them_after_edits(self):
        raw = post(NUMBERED)
        wanted = cl.find_cuts(raw)
        edited = "<p>A new first paragraph the owner added.</p>" + raw
        new, gone = cl.cut(edited, wanted)
        self.assertEqual(len(gone), 7)
        self.assertNotIn("Use-case notes", new)
        self.assertIn("A new first paragraph", new)
        self.assertIn("What the shoe does well", new)
        self.assertIn("Who should buy it", new)


class FakeWP:
    def __init__(self, raw, store_ok=True):
        self.posts = {(7, "posts"): raw}
        self.store_ok = store_ok
        self.saves = 0

    def raw(self, pid, typ):
        return self.posts[(pid, typ)], "2026-11-01T00:00:00"

    def update(self, pid, typ, raw):
        self.saves += 1
        if self.store_ok or self.saves > 1:
            self.posts[(pid, typ)] = raw

    def find(self, url):
        return None


class FakeResp:
    def __init__(self, text):
        self.text = text


class ApplyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.out = patch.object(config, "OUT", Path(self.tmp.name))
        self.out.start()
        self.addCleanup(self.out.stop)
        p = patch.object(cl, "purge", lambda db, site, urls: "no app token")
        p.start()
        self.addCleanup(p.stop)
        s = patch.object(cl.time, "sleep", lambda x: None)
        s.start()
        self.addCleanup(s.stop)
        self.db = new_db()
        self.url = f"https://{S}/nimbus/"
        self.raw = post(NUMBERED)
        self.db.execute("INSERT INTO link_corpus(url, site, post_id, post_type, modified, raw) VALUES(?,?,?,?,?,?)",
                        (self.url, S, 7, "posts", "x", zlib.compress(self.raw.encode())))
        self.item = {"id": cl.cleanup_id(self.url), "site": S, "url": self.url, "status": "approved",
                     "sections": json.dumps(cl.find_cuts(self.raw))}
        self.reports = []

    def report(self, *a):
        self.reports.append(a)

    def live(self, wp, frozen=None):
        return patch.object(cl, "get", lambda url, timeout=None: FakeResp(frozen if frozen is not None else wp.posts[(7, "posts")]))

    def test_apply_cuts_backs_up_verifies_and_undo_restores(self):
        wp = FakeWP(self.raw)
        with self.live(wp):
            touched = cl.apply(self.db, [self.item], {}, {S: wp}, self.report)
        self.assertEqual(touched, {S})
        st, msg = self.reports[-1][1], self.reports[-1][2]
        self.assertEqual(st, "applied")
        self.assertIn("cut 7 sections", msg)
        self.assertIn("gone from the live page", msg)
        self.assertNotIn("Use-case notes", wp.posts[(7, "posts")])
        backups = list((Path(self.tmp.name) / "backups" / "cleanup").glob("*.json"))
        self.assertEqual(json.loads(backups[0].read_text(encoding="utf-8"))["raw_before"], self.raw)
        cl.apply(self.db, [dict(self.item, status="undo")], {}, {S: wp}, self.report)
        self.assertEqual(self.reports[-1][1], "undone")
        self.assertEqual(wp.posts[(7, "posts")], self.raw)

    def test_undo_refuses_when_the_post_was_edited_since(self):
        wp = FakeWP(self.raw)
        with self.live(wp):
            cl.apply(self.db, [self.item], {}, {S: wp}, self.report)
        wp.posts[(7, "posts")] += "<p>owner edit</p>"
        cl.apply(self.db, [dict(self.item, status="undo")], {}, {S: wp}, self.report)
        self.assertEqual(self.reports[-1][1], "failed")
        self.assertIn("edited after the cleanup", self.reports[-1][2])
        self.assertIn("owner edit", wp.posts[(7, "posts")])

    def test_wordpress_not_storing_the_change_restores_the_original(self):
        wp = FakeWP(self.raw, store_ok=False)
        with self.live(wp):
            cl.apply(self.db, [self.item], {}, {S: wp}, self.report)
        self.assertEqual(self.reports[-1][1], "failed")
        self.assertEqual(wp.posts[(7, "posts")], self.raw)

    def test_a_page_builder_page_is_reported_honestly_and_rechecked(self):
        wp = FakeWP(self.raw)
        with self.live(wp, frozen=self.raw):                    # the live page ignores the post body
            cl.apply(self.db, [self.item], {}, {S: wp}, self.report)
            self.assertIn("re-checked every tick", self.reports[-1][2])
            self.db.execute("UPDATE cleanup_ops SET applied_at=datetime('now', '-2 days', '-1 hour')")
            cl.recheck(self.db, self.report)
        self.assertIn("page builder", self.reports[-1][2])

    def test_daily_cap_and_approval_are_required(self):
        wp = FakeWP(self.raw)
        with self.live(wp):
            cl.apply(self.db, [dict(self.item, status="proposed")], {}, {S: wp}, self.report)
            self.assertEqual(self.reports, [])                  # no autopilot: proposed is never applied
            cl.apply(self.db, [self.item], {"cleanups_per_site_per_day": 0}, {S: wp}, self.report)
            self.assertEqual(self.reports, [])
        self.assertEqual(wp.saves, 0)


class AutonomousTests(ApplyTests):
    """Autonomous mode: proposed cleanups are applied without a click, with a canary and a circuit breaker per site."""

    def add_post(self, wp, slug, pid):
        url = f"https://{S}/{slug}/"
        wp.posts[(pid, "posts")] = post(NUMBERED, slug)
        self.db.execute("INSERT INTO link_corpus(url, site, post_id, post_type, modified, raw) VALUES(?,?,?,?,?,?)",
                        (url, S, pid, "posts", "x", zlib.compress(wp.posts[(pid, "posts")].encode())))
        return {"id": cl.cleanup_id(url), "site": S, "url": url, "status": "proposed",
                "sections": json.dumps(cl.find_cuts(wp.posts[(pid, "posts")]))}

    def live_all(self, wp):
        pages = {f"https://{S}/{slug}/": pid for slug, pid in (("nimbus", 7), ("b", 8), ("c", 9))}
        def fake(url, timeout=None):
            pid = pages[url.split("?")[0]]
            return FakeResp(wp.posts[(pid, "posts")])
        return patch.object(cl, "get", fake)

    def test_off_by_default_and_strict_sites_need_their_own_opt_in(self):
        wp = FakeWP(self.raw)
        item = dict(self.item, status="proposed")
        with self.live(wp):
            cl.apply(self.db, [item], {}, {S: wp}, self.report)
            self.assertEqual(wp.saves, 0)
            strict = dict(item, site="gearuptofit.com")
            cl.apply(self.db, [strict], {"autonomous": True}, {"gearuptofit.com": wp}, self.report)
            self.assertEqual(wp.saves, 0)
            cl.apply(self.db, [item], {"autonomous": True}, {S: wp}, self.report)
        self.assertEqual(self.reports[-1][1], "applied")
        self.assertEqual(self.db.execute("SELECT auto FROM cleanup_ops").fetchone()[0], 1)

    def test_sources_safety_and_disclaimers_wait_for_the_owner(self):
        for heading in ("Go deeper: trusted resources", "Sources and further reading", "Final checklist before you act",
                        "What is the safest way to use this page?", "Affiliate disclosure",
                        "Can numerology or astrology prove what will happen?"):
            self.assertTrue(cl.owner_only({"sections": json.dumps([{"heading": heading}])}), heading)
        for heading in ("Use-case notes for ASICS Gel-Nimbus 27 review #1", "How often should I practice with prompts?",
                        "Internal linking and topical authority notes", "Real implementation example"):
            self.assertFalse(cl.owner_only({"sections": [{"heading": heading}]}), heading)
        wp = FakeWP(self.raw)
        secs = json.loads(self.item["sections"]) + [{"heading": "Sources and further reading", "kind": "boilerplate"}]
        held = dict(self.item, status="proposed", sections=json.dumps(secs))
        with self.live(wp):
            cl.apply(self.db, [held], {"autonomous": True}, {S: wp}, self.report)
            self.assertEqual((wp.saves, self.reports), (0, []))
            cl.apply(self.db, [dict(held, status="approved")], {"autonomous": True}, {S: wp}, self.report)
        self.assertEqual(self.reports[-1][1], "applied", "the owner's approval still applies it")

    def test_canary_one_a_day_until_one_is_verified_on_the_live_page(self):
        wp = FakeWP(self.raw)
        items = [dict(self.item, status="proposed"), self.add_post(wp, "b", 8), self.add_post(wp, "c", 9)]
        with patch.object(cl, "get", lambda url, timeout=None: FakeResp(self.raw)):      # live page never changes
            cl.apply(self.db, items, {"autonomous": True}, {S: wp}, self.report)
        self.assertEqual(len(self.reports), 1)                       # the canary only
        with self.live_all(wp):
            cl.apply(self.db, items[1:], {"autonomous": True}, {S: wp}, self.report)
        self.assertEqual(len(self.reports), 1)                       # still not verified: wait for tomorrow
        self.db.execute("DELETE FROM cleanup_ops")
        wp.posts[(7, "posts")] = self.raw                             # a fresh day: the canary post as it was
        with self.live_all(wp):
            cl.apply(self.db, items, {"autonomous": True}, {S: wp}, self.report)
        self.assertIn("gone from the live page", self.reports[1][2])
        self.assertEqual(len(self.reports), 4)                       # canary verified live: the rest follow within the cap

    def test_two_failures_open_the_circuit_breaker_and_raise_an_alert(self):
        wp = FakeWP(self.raw, store_ok=False)
        wp.update = lambda pid, typ, raw: None                       # WordPress never stores anything
        items = [dict(self.item, status="proposed"), self.add_post(wp, "b", 8), self.add_post(wp, "c", 9)]
        with self.live_all(wp):
            cl.apply(self.db, items, {"autonomous": True}, {S: wp}, self.report)
        self.assertEqual([r[1] for r in self.reports], ["failed", "failed"])
        self.assertIsNotNone(cl.breaker(self.db, S))
        alert = self.db.execute("SELECT severity, kind FROM alerts").fetchone()
        self.assertEqual((alert[0], alert[1]), ("high", "autonomous_paused"))
        n = len(self.reports)
        with self.live_all(wp):
            cl.apply(self.db, items, {"autonomous": True}, {S: wp}, self.report)
        self.assertEqual(len(self.reports), n)                       # breaker open: no more autonomous attempts


class ProposeTests(unittest.TestCase):
    def test_one_proposal_per_post_and_ignored_pages_are_skipped(self):
        db = new_db()
        rows = {f"https://{S}/a/": post(NUMBERED), f"https://{S}/b/": post(NUMBERED, "b"), f"https://{S}/c/": post("", "c")}
        for u, raw in rows.items():
            db.execute("INSERT INTO link_corpus(url, site, post_id, post_type, modified, raw) VALUES(?,?,?,?,?,?)",
                       (u, S, 1, "posts", "x", zlib.compress(raw.encode())))
        res = {S: {"buckets": {f"https://{S}/b/": ("INDEXED", ["IGNORED"])}}}
        out = cl.propose(db, res, [S])
        self.assertEqual([x["url"] for x in out], [f"https://{S}/a/"])
        self.assertEqual(len(out[0]["sections"]), 7)
        self.assertEqual(out[0]["id"], cl.cleanup_id(f"https://{S}/a/"))


if __name__ == "__main__":
    unittest.main()
