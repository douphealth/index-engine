"""Speed without changing a decision (the content cache, the narrowed link search, the single-parse cleanup scan,
adaptive audit pacing, one shared Google token) and better prospects (heading phrases, same-subject sources, full
names as anchors, the words a title never says)."""
import random
import sqlite3
import threading
import unittest
import zlib
from unittest.mock import patch

from engine import audit, cleanup, config, google, links, memo, opportunities, recovery, store, util

SITE = "site.test"


def new_db():
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.executescript(store.SCHEMA)
    for sql in store.MIGRATIONS:
        db.execute(sql)
    return db


def put(db, url, raw, site=SITE):
    db.execute("INSERT OR REPLACE INTO link_corpus(url, site, post_id, post_type, modified, raw) VALUES(?,?,?,?,?,?)",
               (url, site, 1, "posts", "2026-11-01T00:00:00", zlib.compress(raw.encode("utf-8"))))


class ContentCacheTest(unittest.TestCase):
    def setUp(self):
        self.db = new_db()
        self.calls = []
        for i in range(3):
            put(self.db, f"https://site.test/p{i}/", f"<p>post number {i}</p>")

    def compute(self, raw):
        self.calls.append(raw)
        return {"words": raw.split(), "n": len(raw)}

    def test_a_post_is_computed_once_and_again_only_when_it_changes(self):
        first = memo.per_post(self.db, SITE, "k", "v1", self.compute)
        self.assertEqual(len(self.calls), 3)
        self.assertEqual(memo.per_post(self.db, SITE, "k", "v1", self.compute), first)
        self.assertEqual(len(self.calls), 3)                              # nothing changed: nothing recomputed
        put(self.db, "https://site.test/p1/", "<p>edited</p>")
        again = memo.per_post(self.db, SITE, "k", "v1", self.compute)
        self.assertEqual(self.calls[3:], ["<p>edited</p>"])               # only the edited post
        self.assertEqual(again["https://site.test/p1/"]["n"], len("<p>edited</p>"))
        self.assertEqual(again["https://site.test/p0/"], first["https://site.test/p0/"])

    def test_a_new_code_version_recomputes_everything(self):
        memo.per_post(self.db, SITE, "k", "v1", self.compute)
        memo.per_post(self.db, SITE, "k", "v2", self.compute)
        self.assertEqual(len(self.calls), 6)

    def test_kinds_and_sites_do_not_share_results_and_removed_posts_are_forgotten(self):
        memo.per_post(self.db, SITE, "a", "v1", self.compute)
        memo.per_post(self.db, SITE, "b", "v1", lambda raw: "other")
        self.assertEqual(memo.per_post(self.db, SITE, "a", "v1", self.compute)["https://site.test/p0/"]["n"], 20)
        self.db.execute("DELETE FROM link_corpus WHERE url='https://site.test/p2/'")
        self.assertNotIn("https://site.test/p2/", memo.per_post(self.db, SITE, "a", "v1", self.compute))
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM content_memo WHERE kind='a'").fetchone()[0], 2)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM content_memo WHERE kind='b'").fetchone()[0], 3)

    def test_the_result_is_the_same_cold_and_warm(self):
        cold = memo.per_post(self.db, SITE, "k", "v1", lambda raw: {"pair": (1, 2), "text": raw})
        warm = memo.per_post(self.db, SITE, "k", "v1", lambda raw: self.fail("recomputed"))
        self.assertEqual(cold, warm)
        self.assertEqual(cold["https://site.test/p0/"]["pair"], [1, 2])   # JSON data both times, never a tuple once

    def test_a_site_result_is_redone_when_any_post_changes(self):
        seen = []
        f = lambda posts: seen.append(len(posts)) or sorted(posts)        # noqa: E731
        first = memo.per_site(self.db, SITE, "s", "v1", f)
        self.assertEqual(memo.per_site(self.db, SITE, "s", "v1", f), first)
        self.assertEqual(seen, [3])
        put(self.db, "https://site.test/p3/", "<p>new</p>")
        self.assertEqual(len(memo.per_site(self.db, SITE, "s", "v1", f)), 4)
        put(self.db, "https://site.test/p3/", "<p>new, edited</p>")
        memo.per_site(self.db, SITE, "s", "v1", f)
        self.assertEqual(seen, [3, 4, 4])
        self.assertIsNone(memo.per_site(self.db, "empty.test", "s", "v1", f))

    def test_a_damaged_row_is_recomputed(self):
        memo.per_post(self.db, SITE, "k", "v1", self.compute)
        self.db.execute("UPDATE content_memo SET data=x'00' WHERE url='https://site.test/p0/'")
        out = memo.per_post(self.db, SITE, "k", "v1", self.compute)
        self.assertEqual(out["https://site.test/p0/"]["n"], 20)
        self.assertEqual(len(self.calls), 4)


WORDS = ("running shoe trail carbon plate marathon recovery foam cushion heel drop tempo long easy interval the of for "
         "and with best new review guide vs women men wide narrow").split()


def random_post(rng):
    parts = []
    for _ in range(rng.randint(3, 9)):
        text = " ".join(rng.choice(WORDS) for _ in range(rng.randint(2, 30)))
        tag = rng.choice(["p", "p", "p", "h2", "li", "blockquote", "figcaption", "td"])
        if rng.random() < 0.15:
            text = text.replace(" ", " <strong>", 1).replace(" ", "</strong> ", 1)
        if rng.random() < 0.1:
            text += " <a href='https://site.test/x/'>" + " ".join(rng.choice(WORDS) for _ in range(3)) + "</a> tail words here"
        parts.append(f"<{tag}>{text}.</{tag}>")
    return "\n".join(parts)


class NarrowedLinkSearchTest(unittest.TestCase):
    def test_reading_only_the_possible_runs_finds_exactly_what_reading_everything_finds(self):
        rng = random.Random(7)
        found_any = 0
        for _ in range(300):
            raw = random_post(rng)
            keys = {tuple(links.stem(rng.choice(WORDS)) for _ in range(rng.randint(2, 5))): "t" for _ in range(25)}
            starts = links.candidate_starts(keys)
            runs = links.possible_runs(links.stem_stream(raw), keys, starts)
            whole = links.candidates(raw, keys, starts)
            self.assertEqual(links.candidates(raw, keys, starts, runs), whole)
            found_any += bool(whole)
            if not runs:
                self.assertEqual(whole, {})
        self.assertGreater(found_any, 50)                                 # the comparison is not vacuous

    def test_the_stream_survives_the_cache(self):
        db = new_db()
        raw = "<p>Readers who train for a marathon often ask which carbon plate shoe lasts longest on the road.</p>"
        put(db, "https://site.test/a/", raw)
        cached = memo.per_post(db, SITE, "stem_stream", links.STEMS_VERSION, links.stem_stream)["https://site.test/a/"]
        keys = {("carbon", "plate", "shoe"): "t"}
        starts = links.candidate_starts(keys)
        runs = links.possible_runs(cached, keys, starts)
        self.assertEqual(links.candidates(raw, keys, starts, runs), links.candidates(raw, keys, starts))
        self.assertEqual(raw[slice(*links.candidates(raw, keys, starts, runs)[("carbon", "plate", "shoe")])], "carbon plate shoe")


def para(i, n=40):
    return "<p>" + " ".join(f"word{i}x{j}" for j in range(n)) + ".</p>"


BOILER = "<h2>How we review every product here</h2><p>" + " ".join(f"same{j}" for j in range(40)) + ".</p>"


class CleanupScanTest(unittest.TestCase):
    def posts(self):
        out = {}
        for i in range(8):
            out[f"https://site.test/p{i}/"] = (f"<h2>Intro {i}</h2>{para(i)}{BOILER}<h2>Closing thoughts on item {i}</h2>{para(100 + i)}")
        out["https://site.test/own/"] = "<h2>How we review every product here</h2>" + para(999) + "<h2>End</h2>" + para(998)
        out["https://site.test/numbered/"] = "".join(
            f"<h2>Use-case notes for the trail shoe review #{k}</h2>{para(200 + k, 12)}" for k in range(1, 5)) + "<h2>End</h2>" + para(997)
        return out

    def test_one_parse_per_post_decides_what_two_parses_decided(self):
        posts = self.posts()
        boiler = cleanup.boilerplate_index(list(posts.values()))
        old = [[u, cuts] for u, raw in posts.items() for cuts in [cleanup.find_cuts(raw, boiler)] if cuts]
        new = cleanup.site_cuts(posts)
        self.assertEqual(new, old)
        kinds = {u.rsplit("/", 2)[1]: [c["kind"] for c in cuts] for u, cuts in new}
        self.assertEqual(kinds["p0"], ["boilerplate"])
        self.assertEqual(kinds["numbered"], ["numbered"] * 4)
        self.assertNotIn("own", kinds)                                    # same heading, its own text: kept

    def test_shingles_are_runs_of_five_words(self):
        self.assertEqual(cleanup._shingles(""), set())
        self.assertEqual(len(cleanup._shingles("one two three")), 1)
        self.assertEqual(len(cleanup._shingles("a b c d e")), 1)
        self.assertEqual(len(cleanup._shingles("a b c d e f g")), 3)
        self.assertEqual(cleanup._similar(cleanup._shingles("a b c d e f g"), cleanup._shingles("A, b c d e f. G!")), 1.0)

    def test_proposals_come_from_the_cache_until_a_post_changes(self):
        db = new_db()
        for u, raw in self.posts().items():
            put(db, u, raw)
        with patch.object(cleanup, "log"):
            first = cleanup.propose(db, {}, [SITE])
            with patch.object(cleanup, "site_cuts", side_effect=AssertionError("recomputed")):
                self.assertEqual(cleanup.propose(db, {}, [SITE]), first)
            put(db, "https://site.test/numbered/", "<h2>Just one section now</h2>" + para(5))
            after = cleanup.propose(db, {}, [SITE])
        self.assertEqual(len(first), 9)
        self.assertEqual(len(after), 8)
        self.assertNotIn("https://site.test/numbered/", {c["url"] for c in after})

    def test_an_ignored_page_is_left_out_without_recomputing(self):
        db = new_db()
        for u, raw in self.posts().items():
            put(db, u, raw)
        with patch.object(cleanup, "log"):
            everything = cleanup.propose(db, {}, [SITE])
            some = cleanup.propose(db, {SITE: {"buckets": {"https://site.test/p0/": ("INDEXED", ["IGNORED"])}}}, [SITE])
        self.assertEqual(len(some), len(everything) - 1)


class TemplateScanCacheTest(unittest.TestCase):
    def test_cold_and_warm_scans_agree_and_follow_edits(self):
        db = new_db()
        for i in range(6):
            put(db, f"https://site.test/p{i}/", "".join(f"<h2>Use-case notes for the trail shoe review #{k}</h2><p>x</p>" for k in range(1, 4))
                + "<h3>Visual Examples From the WordPress Media Library</h3>")
        cold = recovery.template_scan(db, SITE)
        with patch.object(recovery, "post_scan", side_effect=AssertionError("recomputed")):
            self.assertEqual(recovery.template_scan(db, SITE), cold)
        self.assertEqual(len(cold["pages"]), 6)
        put(db, "https://site.test/p0/", "<h2>A normal heading</h2><p>Fine.</p>")
        self.assertEqual(len(recovery.template_scan(db, SITE)["pages"]), 5)


def answer(status=200, ms=100, error=None):
    return {"status": status, "ms": ms, "error": error}


class AuditPaceTest(unittest.TestCase):
    def test_a_fast_site_earns_more_requests_one_at_a_time_up_to_the_cap(self):
        p = audit.Pace("fast.test")
        self.assertEqual(p.limit, config.PER_HOST_CONCURRENCY)
        for _ in range(config.AUDIT_RAMP_AFTER - 1):
            p.saw(answer())
        self.assertEqual(p.limit, config.PER_HOST_CONCURRENCY)
        p.saw(answer())
        self.assertEqual(p.limit, config.PER_HOST_CONCURRENCY + 1)
        for _ in range(500):
            p.saw(answer(status=404))                                     # a quick 404 is still a quick answer
        self.assertEqual(p.limit, config.PER_HOST_MAX)

    def test_trouble_halves_it_at_once_and_caps_it_for_the_run(self):
        p = audit.Pace("busy.test")
        for _ in range(500):
            p.saw(answer())
        p.saw(answer(status=503))
        self.assertEqual(p.limit, config.PER_HOST_MAX // 2)
        for _ in range(500):
            p.saw(answer())
        self.assertEqual(p.limit, config.PER_HOST_MAX - 1)                # never back to the level that hurt
        for bad in (answer(status=None, error="Timeout"), answer(status=429), answer(status=403, error="bot challenge")):
            q = audit.Pace("x.test")
            q.saw(bad)
            self.assertEqual(q.limit, 1)

    def test_a_merely_slow_site_is_read_as_before(self):
        p = audit.Pace("slow.test")
        for _ in range(50):
            p.saw(answer(ms=config.AUDIT_SLOW_MS + 1))
        self.assertEqual(p.limit, config.PER_HOST_CONCURRENCY)            # never below the default for slowness alone
        for _ in range(50):
            p.saw(answer(ms=config.AUDIT_FAST_MS + 1))                    # neither fast nor slow: stays put
        self.assertEqual(p.limit, config.PER_HOST_CONCURRENCY)

    def test_a_listed_slow_host_keeps_its_fixed_number(self):
        with patch.dict(config.SLOW_HOSTS, {"pinned.test": 2}):
            p = audit.Pace("pinned.test")
            for _ in range(200):
                p.saw(answer())
            self.assertEqual(p.limit, 2)


class FakeCredentials:
    def __init__(self):
        self.valid, self.refreshes = False, 0

    def refresh(self, request):
        self.refreshes += 1
        self.valid = True


class SharedGoogleTokenTest(unittest.TestCase):
    def test_every_thread_shares_one_token_and_one_renewal(self):
        fake = FakeCredentials()
        with patch("google.oauth2.service_account.Credentials.from_service_account_info", return_value=fake) as build:
            client = google.GSCClient({"type": "service_account"})
            got = []

            def worker():
                creds = client._credentials()
                if not creds.valid:
                    creds.refresh(None)
                creds.refresh(None)                                       # what a 401 retry does, from every thread at once
                got.append(creds)

            threads = [threading.Thread(target=worker) for _ in range(24)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
        self.assertEqual(build.call_count, 1)
        self.assertTrue(all(c is fake for c in got))
        self.assertEqual(fake.refreshes, 1)

    def test_inspection_threads_are_capped(self):
        self.assertGreaterEqual(config.INSPECT_WORKERS_PER_PROPERTY, 8)
        self.assertLessEqual(config.INSPECT_WORKERS_PER_PROPERTY * 0 + config.INSPECT_WORKERS_MAX, 64)


class StageTimingTest(unittest.TestCase):
    def test_the_report_lists_the_slowest_stage_first(self):
        util.stage_report()
        with patch.object(util.time, "time", side_effect=[0, 12, 20, 200, 300, 300.2]):
            with util.stage("audit"):
                pass
            with util.stage("Google inspection"):
                pass
            with util.stage("report"):
                pass
        line = util.stage_report()
        self.assertTrue(line.startswith("[time] 3.2 min in all: Google inspection 3.0 min, audit 12 s"), line)
        self.assertNotIn("report", line)                                  # under a second: not worth a mention
        self.assertEqual(util.stage_report(), "[time] 0 s in all")


TARGET = "https://dogs.test/tail-pocket-cleaning/"
RELATED = "https://dogs.test/skin-fold-cleaning/"
POPULAR = "https://dogs.test/dog-food-brands/"
SHOE = "https://dogs.test/balance-foam/"
MENTION = ("<p>Owners who skip the weekly tail pocket cleaning usually find out the hard way, when the skin under the "
           "tail turns red and sore.</p>")


class FakeWP:
    ok = True

    def __init__(self, texts):
        self.texts = texts

    def corpus(self, after=None):
        for i, (url, raw) in enumerate(self.texts.items()):
            yield {"url": url, "raw": raw, "modified": "2026-11-01T00:00:00", "id": i + 1, "type": "posts"}


class BetterLinkProspectsTest(unittest.TestCase):
    def setUp(self):
        self.db = new_db()
        self.pages = {TARGET: ("Tail Pocket Cleaning: A Vet-Checked Routine", "", "UNKNOWN_TO_GOOGLE", 0),
                      RELATED: ("Skin Fold Cleaning for Wrinkly Breeds", "", "INDEXED", 3),
                      POPULAR: ("Dog Food Brands Compared by Price", "", "INDEXED", 12)}
        self.texts = {TARGET: "<p>The target page has its own text and no link to any of the other pages at all.</p>",
                      RELATED: MENTION, POPULAR: MENTION}
        for i in range(9):                                                # enough pages that no single word is site-wide
            u = f"https://dogs.test/filler-{i}/"
            self.pages[u] = (f"Unrelated Subject Number{i} Alpha{i} Beta{i}", "", "INDEXED", 1)
            self.texts[u] = f"<p>Filler page {i} talks about alpha{i} and beta{i} and nothing else worth a mention here.</p>"
        patcher = patch.object(links, "log")
        patcher.start()
        self.addCleanup(patcher.stop)

    def propose(self, gain=None):
        for u, (title, h1, _b, _c) in self.pages.items():
            self.db.execute("INSERT OR REPLACE INTO audits(url, title, h1) VALUES(?,?,?)", (u, title, h1))
        buckets = {u: (b, set()) for u, (_t, _h, b, _c) in self.pages.items()}
        rows = [{"url": u, "kind": "post", "gsc_clicks": c, "bing_clicks": 0} for u, (_t, _h, _b, c) in self.pages.items()]
        results = {"dogs.test": {"buckets": buckets, "rows": rows, "opportunities": {"items": []}}}
        return links.propose(self.db, results, gain or {TARGET: 1.0}, {"dogs.test": FakeWP(self.texts)})

    def test_a_source_on_the_same_subject_outranks_a_stronger_unrelated_one(self):
        out = self.propose()
        self.assertEqual([p["source"] for p in out], [RELATED, POPULAR])
        self.assertEqual(out[0]["anchor"], "tail pocket cleaning")
        self.assertIn("on the same subject (both are about cleaning)", out[0]["why"])
        self.assertNotIn("same subject", out[1]["why"])
        self.assertGreater(out[0]["score"], out[1]["score"])
        self.assertEqual(links.relatedness({"tail", "pocket"}, {"dog", "food"}, {}, 10), 0.0)
        self.assertEqual(links.relatedness({"tail", "pocket"}, {"tail", "pocket", "x"}, {}, 10), 1.0)
        one_rare = links.relatedness({"tail", "pocket"}, {"tail", "x"}, {"tail": 1}, 100)
        one_common = links.relatedness({"tail", "pocket"}, {"tail", "x"}, {"tail": 20}, 100)
        self.assertTrue(0 < one_common < one_rare < 1)

    def test_the_main_heading_names_the_page_too(self):
        self.pages[TARGET] = ("Cleaning Routine: A Vet-Checked Plan", "Tail Pocket Cleaning", "UNKNOWN_TO_GOOGLE", 0)
        self.assertEqual({p["anchor"] for p in self.propose()}, {"tail pocket cleaning"})
        self.pages[TARGET] = ("Cleaning Routine: A Vet-Checked Plan", "", "UNKNOWN_TO_GOOGLE", 0)
        self.assertEqual(self.propose(), [])                               # without the heading nothing names it
        self.pages[TARGET] = ("A Vet-Checked Plan", "Tail Pocket Cleaning", "UNKNOWN_TO_GOOGLE", 0)
        self.assertEqual(self.propose(), [])                               # a heading about something the title is not: unused
        chunks, _slug = links.page_names(TARGET, "Same Words", "Same Words")
        self.assertEqual(chunks, [["same", "word"]])                       # a heading equal to the title adds nothing

    def test_a_heading_phrase_another_page_also_has_is_not_an_anchor(self):
        self.pages[TARGET] = ("Cleaning Routine: A Vet-Checked Plan", "Tail Pocket Cleaning", "UNKNOWN_TO_GOOGLE", 0)
        self.pages[POPULAR] = ("Dog Food Brands Compared by Price", "Tail Pocket Cleaning Mistakes", "INDEXED", 12)
        self.assertEqual(self.propose(), [])

    def test_a_heading_only_rewords_the_name_it_never_adds_to_it(self):
        goals = "https://dogs.test/goal-achievement/"
        self.pages[goals] = ("Goal Achievement: A Framework to Finish Goals", "Goal Achievement: How to Turn Goals Into Results",
                             "UNKNOWN_TO_GOOGLE", 0)
        self.texts[goals] = "<p>This page has its own text about finishing and no link to any other page at all.</p>"
        self.texts[POPULAR] = "<p>People who turn goals into a weekly routine tend to keep them far longer than the rest of us.</p>"
        self.assertEqual(self.propose({goals: 1.0}), [])                   # "turn" is the heading's phrasing, not the page's name
        self.assertTrue(links.heading_only(("turn", "goal"), {}, {"goal", "achiev", "framework", "finish"}))
        self.assertFalse(links.heading_only(("burnout", "symptom"), {}, {"burnout", "work", "sign", "symptom"}))
        self.assertFalse(links.heading_only(("turn", "goal"), {("turn", "goal"): True}, set()))     # the title offers it itself

    def test_a_phrase_cut_off_before_an_article_is_not_an_anchor(self):
        chunks, _slug = links.title_words("https://dogs.test/x/", "How Long Does It Take to Run a Mile")
        offered = {" ".join(k) for k in links.title_phrases(chunks, set())}
        self.assertNotIn("take to run", offered)
        self.assertIn("run a mile", offered)

    def test_the_anchor_takes_the_full_name_when_the_text_says_it(self):
        self.pages[SHOE] = ("New Balance Fresh Foam Review", "", "UNKNOWN_TO_GOOGLE", 0)
        self.texts[SHOE] = "<p>Shoe page text.</p>"
        self.texts[RELATED] = ("<p>Walkers who tried the New Balance Fresh Foam last spring said the cushioning felt much "
                               "softer than the pair it replaced.</p>")
        self.texts[POPULAR] = ("<p>Walkers who compared it with the older Balance Fresh Foam last spring said the cushioning "
                               "felt much softer than before.</p>")
        out = {p["source"]: p["anchor"] for p in self.propose({SHOE: 1.0})}
        self.assertEqual(out, {RELATED: "New Balance Fresh Foam", POPULAR: "Balance Fresh Foam"})
        self.assertEqual(links.lead_word([["how", "long", "doe", "it", "take", "to", "run"]], ("take", "to", "run")), None)

    def test_a_page_bing_ranks_but_google_has_not_indexed_gets_search_anchors(self):
        names = {TARGET: links.page_names(TARGET, "Tail Pocket Cleaning: A Vet-Checked Routine")}
        opp = {"items": [{"kind": "close_to_top", "src": "bing", "url": TARGET, "gain": 20.0,
                          "queries": [{"query": "clean bulldog tail pocket", "pos": 6.0, "impr": 300, "clicks": 1, "gain": 20.0}]}]}
        for bucket, expected in (("INDEXED", 1), ("CRAWLED_NOT_INDEXED", 1), ("NOINDEX", 0)):
            keys, gains = links.growth_targets(self.db, "dogs.test", opp, {TARGET: (bucket, set())}, names, {}, set())
            self.assertEqual(len(gains), expected, bucket)


class TitleGapTest(unittest.TestCase):
    def test_missing_words_are_the_search_words_the_title_and_heading_never_say(self):
        self.assertEqual(opportunities.missing_words("mouse poop", "What Do Mouse Droppings Look Like?", ""), ["poop"])
        self.assertEqual(opportunities.missing_words("how to clean backpacks", "How to wash a backpack", "Cleaning tips"), ["clean"])
        self.assertEqual(opportunities.missing_words("how to clean backpacks", "How to wash a backpack", "Clean it right"), [])
        self.assertEqual(opportunities.missing_words("the best of", "Anything", ""), [])      # no topic word, nothing missing

    def test_the_advice_names_the_gap_for_short_searches_only(self):
        db = new_db()
        url = "https://mice.test/droppings/"
        db.execute("INSERT INTO audits(url, title, h1) VALUES(?,?,?)", (url, "What Do Mouse Droppings Look Like?", "Mouse Droppings"))
        db.execute("INSERT INTO urls(url, site) VALUES(?, 'mice.test')", (url,))
        long_query = "what does mouse poop look like in a kitchen cupboard"
        for q, pos in (("mouse poop", 6.0), (long_query, 7.0)):
            db.execute("INSERT INTO page_queries(src, url, site, query, clicks, impr, pos, win_end) VALUES('google',?,?,?,?,?,?,?)",
                       (url, "mice.test", q, 1, 900, pos, "2026-11-01"))
        curve = [0.3, 0.2, 0.12, 0.05, 0.04, 0.03, 0.02] + [0.01] * 13
        opp = opportunities.analyze(db, "mice.test", {url: ("INDEXED", [])}, prior={"google": curve, "bing": curve})
        item = next(x for x in opp["items"] if x["kind"] == "close_to_top")
        self.assertEqual(item["query"], "mouse poop")
        self.assertEqual(item["missing"], ["poop"])
        self.assertIn("never say “poop”", item["advice"])
        self.assertEqual(sorted(len(q["query"].split()) for q in item["queries"]), [2, 10])
        self.assertNotIn("cupboard”", item["advice"])


if __name__ == "__main__":
    unittest.main()
