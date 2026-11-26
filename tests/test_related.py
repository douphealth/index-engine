"""Pages on the same subject are found by what they say, not only by the words of their titles (engine/related.py)."""
import importlib.util
import math
import random
import sqlite3
import unittest
import zlib
from unittest.mock import patch

from engine import linkgaps, links, memo, related, store

SITE = "example.test"
B = "https://" + SITE


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


def random_counts(rng, pages=40, vocab=120):
    words = [f"w{i}" for i in range(vocab)]
    return {f"{B}/p{i}/": {w: rng.randint(1, 9) for w in rng.sample(words, rng.randint(1, 30))} for i in range(pages)}


class PageTerms(unittest.TestCase):
    def test_counts_the_visible_words_as_stems(self):
        raw = ("<h2>Trail Shoes</h2><p>Trail shoes grip. A shoe&nbsp;for the trails.</p>"
               "<script>var shoes = 'tracking';</script><style>.shoe{color:red}</style><!-- shoes -->"
               "[gallery ids=\"1,2\" shoes=\"yes\"]<a href='/x/'>Waterproof</a>")
        self.assertEqual(related.page_terms(raw), {"shoe": 3, "trail": 3, "grip": 1, "waterproof": 1})

    def test_keeps_the_most_used_words_only(self):
        raw = "<p>" + " ".join(f"word{chr(97 + i % 26)}{i}x " * (i + 1) for i in range(300)) + "</p>"
        with patch.object(related, "MAX_TERMS", 50):
            got = related.page_terms(raw)
        self.assertEqual(len(got), 50)
        self.assertEqual(min(got.values()), 251)                 # the 50 most used of 300, used 251..300 times

    def test_is_plain_data_the_cache_can_store(self):
        got = related.page_terms("<p>Alpha beta alpha.</p>")
        self.assertEqual(memo._unpack(memo._pack(got)), got)


class TheIndex(unittest.TestCase):
    def test_weights_follow_the_written_formula(self):
        # 4 pages; "run" is on 2 of them (kept), "only" on 1 (dropped: MIN_DF), "site" on 3 (dropped: more than half)
        counts = {"a": {"run": 3, "shoe": 1, "site": 1, "only": 5}, "b": {"run": 1, "shoe": 2, "site": 1},
                  "c": {"yoga": 2, "site": 4, "mat": 1}, "d": {"yoga": 1, "mat": 1}}
        ix = related.Index(counts)
        idf = math.log(5 / 3) + 1                               # every kept word is on 2 of the 4 pages
        a = {"run": (1 + math.log(3)) * idf, "shoe": 1 * idf}
        b = {"run": 1 * idf, "shoe": (1 + math.log(2)) * idf}
        dot = sum(a[t] * b[t] for t in a) / math.sqrt(sum(x * x for x in a.values())) / math.sqrt(sum(x * x for x in b.values()))
        self.assertAlmostEqual(ix.cosine("a", "b"), dot, places=12)
        self.assertEqual(set(ix.vec["a"]), {"run", "shoe"})
        self.assertAlmostEqual(sum(x * x for x in ix.vec["c"].values()), 1.0, places=12)

    def test_cosine_is_symmetric_bounded_and_zero_for_strangers(self):
        ix = related.Index(random_counts(random.Random(4)))
        urls = sorted(ix.vec)
        for a in urls[:8]:
            for b in urls[:8]:
                self.assertAlmostEqual(ix.cosine(a, b), ix.cosine(b, a), places=12)
                self.assertTrue(-1e-12 <= ix.cosine(a, b) <= 1 + 1e-12)
            if a in ix:
                self.assertAlmostEqual(ix.cosine(a, a), 1.0, places=12)
        self.assertEqual(ix.cosine(urls[0], "https://elsewhere.test/"), 0.0)
        self.assertNotIn("https://elsewhere.test/", ix)

    def test_nearest_is_the_brute_force_answer(self):
        ix = related.Index(random_counts(random.Random(5), pages=60))
        urls = sorted(ix.vec)
        for a in urls[:10]:
            brute = sorted(((ix.cosine(a, b), b) for b in urls if b != a and ix.cosine(a, b) > 0), key=lambda su: (-su[0], su[1]))
            got = ix.nearest(a, k=7)
            self.assertEqual([u for _, u in got], [u for _, u in brute[:7]])
            for (x, _), (y, _) in zip(got, brute):
                self.assertAlmostEqual(x, y, places=12)
            few = set(urls[20:30])
            self.assertTrue(all(u in few for _, u in ix.nearest(a, among=few, k=50)))
        self.assertEqual(ix.nearest("https://elsewhere.test/"), [])

    def test_shared_names_the_words_that_tie_two_pages(self):
        counts = {"a": {"garmin": 9, "battery": 1, "strap": 1}, "b": {"garmin": 7, "battery": 2, "map": 1},
                  "c": {"strap": 2, "map": 3, "yoga": 1}, "d": {"yoga": 4, "mat": 2}, "e": {"mat": 1, "block": 1}, "f": {"block": 2}}
        ix = related.Index(counts)
        self.assertEqual(ix.shared("a", "b"), ["garmin", "battery"])
        self.assertEqual(ix.shared("a", "f"), [])

    def test_high_is_the_sites_own_bar_and_the_same_every_time(self):
        counts = random_counts(random.Random(6), pages=50)
        one, two = related.Index(counts), related.Index(dict(reversed(list(counts.items()))))
        self.assertEqual(one.high(), two.high())
        urls = sorted(counts)
        pairs = sorted(one.cosine(a, b) for i, a in enumerate(urls) for b in urls[i + 1:])
        self.assertTrue(pairs[int(0.8 * len(pairs))] <= one.high() <= pairs[int(0.97 * len(pairs))])
        self.assertEqual(related.Index({}).high(), 0.0)

    @unittest.skipUnless(importlib.util.find_spec("sklearn"), "scikit-learn is not installed here (CI installs it)")
    def test_every_number_equals_scikit_learns(self):
        from sklearn.feature_extraction.text import TfidfVectorizer
        for seed in range(5):
            counts = random_counts(random.Random(seed), pages=30 + 10 * seed, vocab=60 + 40 * seed)
            urls = sorted(counts)
            docs = [[t for t, k in counts[u].items() for _ in range(k)] for u in urls]
            x = TfidfVectorizer(analyzer=lambda d: d, sublinear_tf=True, min_df=related.MIN_DF, max_df=related.MAX_DF).fit_transform(docs)
            ref = (x @ x.T).toarray()
            ix = related.Index(counts)
            worst = max(abs(ref[i][j] - ix.cosine(a, b)) for i, a in enumerate(urls) for j, b in enumerate(urls) if i != j)
            self.assertLess(worst, 1e-12)


class Blend(unittest.TestCase):
    def test_is_a_zero_to_one_score_that_rises_with_either_part(self):
        self.assertEqual(related.blend(0, 0), 0.0)
        self.assertEqual(related.blend(1, 1), 1.0)
        self.assertEqual(related.blend(1, 0.9), 1.0)                                  # capped
        self.assertLess(related.blend(0.2, 0.1), related.blend(0.3, 0.1))
        self.assertLess(related.blend(0.2, 0.1), related.blend(0.2, 0.2))
        # The same title overlap, but one page also reads like the target: it wins.
        self.assertGreater(related.blend(0.4, 0.30), related.blend(0.4, 0.05))
        # The text outweighs the name: a page that reads alike beats one that only shares a rare title word.
        self.assertGreater(related.blend(0.0, 0.35), related.blend(0.6, 0.05))


class SiteIndex(unittest.TestCase):
    def setUp(self):
        self.db = new_db()
        self.addCleanup(self.db.close)

    def test_a_site_without_enough_stored_posts_has_none(self):
        self.assertIsNone(related.site_index(self.db, SITE))
        for i in range(related.MIN_PAGES - 1):
            put(self.db, f"{B}/p{i}/", f"<p>Post {i} about running shoes and trails.</p>")
        self.assertIsNone(related.site_index(self.db, SITE))

    def test_words_are_counted_once_per_post_until_it_changes(self):
        for i in range(10):
            put(self.db, f"{B}/p{i}/", f"<p>Post number{i} about running shoes and trails number{i}.</p>")
        with patch.object(related, "page_terms", side_effect=related.page_terms) as counted:
            first = related.site_index(self.db, SITE)
            self.assertEqual(counted.call_count, 10)
            again = related.site_index(self.db, SITE)
            self.assertEqual(counted.call_count, 10)                 # read back, not recounted
            put(self.db, f"{B}/p3/", "<p>Rewritten entirely: yoga mats and blocks.</p>")
            related.site_index(self.db, SITE)
            self.assertEqual(counted.call_count, 11)
        self.assertEqual(first.vec, again.vec)


REVIEW = ("<p>Battery life in GPS mode, the AMOLED display, solar charging, maps and navigation on the wrist, heart rate "
          "accuracy on intervals, and how the {name} watch compares for trail runners who need multi-band GPS.</p>")
SHOE = ("<p>Midsole foam, heel drop, outsole rubber and upper fit: how the {name} daily trainer shoe rides on easy miles, "
        "tempo runs and long runs, with stack height and weight compared.</p>")


class LinkGapsReadTheText(unittest.TestCase):
    """An unindexed watch review titled without any word the other watch reviews use in their titles."""

    def setUp(self):
        self.db = new_db()
        self.addCleanup(self.db.close)
        self.buckets = {}

    def page(self, slug, title, bucket, raw=None, value=1.0):
        url = f"{B}/{slug}/"
        self.db.execute("INSERT INTO urls(url, site, kind, in_sitemap) VALUES (?,?,'post',1)", (url, SITE))
        self.db.execute("INSERT INTO audits(url, status, title) VALUES (?,200,?)", (url, title))
        self.db.execute("INSERT INTO triage(url, site, bucket, flags, value) VALUES (?,?,?,'',?)", (url, SITE, bucket, value))
        self.buckets[url] = (bucket, "")
        if raw is not None:
            put(self.db, url, raw)
        return url

    def site(self, stored=True):
        raw = (lambda text: text) if stored else (lambda text: None)
        self.target = self.page("wrist-computer-worth-it", "Is the Enduro Worth It? A Trainer for Ultra Days", "CRAWLED_NOT_INDEXED",
                                raw(REVIEW.format(name="Enduro")), value=9)
        self.watches = [self.page(f"watch-{n}", f"{n.title()} Review", "INDEXED", raw(REVIEW.format(name=n)))
                        for n in ("fenix", "instinct", "forerunner")]
        self.shoes = [self.page(f"shoe-{n}", f"{n.title()} Daily Trainer Review", "INDEXED", raw(SHOE.format(name=n)))
                      for n in ("clifton", "pegasus", "novablast", "ghost", "ride", "kinvara")]
        foods = ["oats", "lentils", "salmon", "quinoa", "yogurt", "spinach", "almonds", "berries", "tofu", "eggs"]
        for i in range(30):                                      # the rest of the site: recipes, each a little like two others
            self.page(f"other-{i}", f"Unrelated{i} Matter{i}", "INDEXED",
                      raw(f"<p>Recipe {i}: {foods[i % 10]} with {foods[(i + 3) % 10]}, a smoothie and a note on sleep.</p>"))

    def sources(self):
        g = linkgaps.analyze(self.db, SITE, self.buckets)
        item, = [x for x in g["items"] if x["url"] == self.target]
        return item["sources"]

    def test_pages_that_read_alike_are_suggested_and_a_shared_title_word_alone_is_not(self):
        self.site()
        got = self.sources()
        self.assertEqual({s["url"] for s in got}, set(self.watches))          # no title word in common with any of them
        self.assertFalse({s["url"] for s in got} & set(self.shoes))           # "trainer" is in six shoe titles
        self.assertTrue(all(s["shared"] for s in got))
        self.assertTrue(all(0 < s["related"] <= 1 for s in got))

    def test_without_stored_text_the_names_decide_as_before(self):
        self.site(stored=False)
        got = {s["url"] for s in self.sources()}
        self.assertEqual(len(got), linkgaps.MAX_SOURCES)
        self.assertTrue(got <= set(self.shoes))                  # only the title word "trainer" ties anything to it

    def test_a_page_already_linking_is_not_suggested(self):
        self.site()
        self.db.execute("INSERT INTO links(src, dst, in_content) VALUES (?,?,0)", (self.watches[0], self.target))
        self.assertEqual({s["url"] for s in self.sources()}, set(self.watches[1:]))


class FakeWP:
    ok = True

    def __init__(self, texts):
        self.texts = texts

    def corpus(self, after=None):
        for i, (url, raw) in enumerate(self.texts.items()):
            yield {"url": url, "raw": raw, "modified": "2026-11-01T00:00:00", "id": i + 1, "type": "posts"}


class LinkProposalsReadTheText(unittest.TestCase):
    def test_of_two_pages_that_say_the_phrase_the_one_on_the_subject_links_first(self):
        db = new_db()
        self.addCleanup(db.close)
        target = f"{B}/tail-pocket-cleaning/"
        vet, food = f"{B}/wrinkle-care/", f"{B}/kibble-prices/"
        say = "Owners often ask about tail pocket cleaning when a bulldog keeps scooting across the floor. "
        skin = ("Skin folds trap moisture and yeast: wipe the folds with a chlorhexidine wipe, dry them fully, and watch for "
                "redness, odor and infection around the tail and the face.")
        pages = {target: ("Tail Pocket Cleaning: A Vet-Checked Routine", "UNKNOWN_TO_GOOGLE", 0,
                          "<p>Wipe the pocket with a chlorhexidine wipe, dry the folds fully, and check for redness, odor, yeast "
                          "and infection each week.</p>"),
                 vet: ("Keeping a Bulldog Comfortable", "INDEXED", 2, f"<p>{say}{skin}</p>"),
                 food: ("Kibble Compared by Price", "INDEXED", 9,
                        f"<p>{say}Kibble price per pound, protein percentage, bag sizes and the brands compared by cost.</p>")}
        for i in range(9):
            pages[f"{B}/filler-{i}/"] = (f"Unrelated Subject Number{i} Alpha{i}", "INDEXED", 1,
                                         f"<p>Filler page {i} talks about alpha{i} and beta{i} and nothing else worth a mention.</p>")
        for u, (title, _b, _c, _raw) in pages.items():
            db.execute("INSERT INTO audits(url, title, h1) VALUES(?,?,'')", (u, title))
        buckets = {u: (b, set()) for u, (_t, b, _c, _r) in pages.items()}
        rows = [{"url": u, "kind": "post", "gsc_clicks": c, "bing_clicks": 0} for u, (_t, _b, c, _r) in pages.items()]
        results = {SITE: {"buckets": buckets, "rows": rows, "opportunities": {"items": []}}}
        with patch.object(links, "log"):
            out = links.propose(db, results, {target: 1.0}, {SITE: FakeWP({u: p[3] for u, p in pages.items()})})
        # Neither title shares a word with the target's, and the food page has four times the clicks.
        self.assertEqual([p["source"] for p in out], [vet, food])
        self.assertIn("covers the same subject in its text", out[0]["why"])
        self.assertNotIn("same subject", out[1]["why"])


if __name__ == "__main__":
    unittest.main()
