"""Many small searches for one page are one prospect.

From 26 Nov 2026: 58% of the portfolio's search impressions (23,895 of 41,273) were in page/search pairs with fewer
than MIN_IMPR impressions, which no opportunity kind looked at. A page shown at position 8 for 74 wordings of "best
over the counter weight loss pills", 292 impressions together, was listed nowhere."""
import random
import sqlite3
import unittest

from engine import opportunities as op, store

SITE = "site.example"
URL = f"https://{SITE}/mice-climb/"
CURVE = [0.30, 0.15, 0.10, 0.10, 0.05, 0.05] + [0.02] * 4 + [0.01] * 10
CURVES = {"bing": CURVE, "google": CURVE}


def row(query, impr, pos, clicks=0, url=URL, src="bing", title="Can Mice Climb Walls?", h1="Can mice climb walls?"):
    return {"src": src, "url": url, "query": query, "clicks": clicks, "impr": impr, "pos": pos, "title": title, "h1": h1,
            "description": ""}


CLIMB = [row("can mice climb", 20, 6.0), row("can a mouse climb a wall", 12, 7.0), row("can mice climb walls vertically", 9, 8.0),
         row("can mice climb up walls", 6, 9.0)]


class LongTail(unittest.TestCase):
    def test_related_small_searches_become_one_item_for_the_page(self):
        found = op.long_tail(CLIMB, CURVES, set())
        self.assertEqual(len(found), 1)
        x = found[0]
        self.assertEqual((x["kind"], x["src"], x["url"], x["searches"], x["impr_total"]), ("long_tail", "bing", URL, 4, 47))
        self.assertEqual(x["query"], "can mice climb")                 # the biggest search leads
        self.assertAlmostEqual(x["pos"], (20 * 6 + 12 * 7 + 9 * 8 + 6 * 9) / 47, places=1)
        # The gain is close_to_top's estimate summed: impressions x (click-through at position 3 - at its own position).
        want = 20 * (0.10 - 0.05) + 12 * (0.10 - 0.02) + 9 * (0.10 - 0.02) + 6 * (0.10 - 0.02)
        self.assertAlmostEqual(x["gain"], round(want, 1), places=1)
        self.assertAlmostEqual(sum(q["gain"] for q in x["queries"]), x["gain"], delta=0.2)
        self.assertIn("4 related searches", x["advice"])
        self.assertIn("47 times", x["advice"])

    def test_a_search_big_enough_alone_is_left_to_close_to_top(self):
        rows = CLIMB + [row("can mice climb stairs", op.MIN_IMPR, 6.0)]
        x = op.long_tail(rows, CURVES, set())[0]
        self.assertEqual(x["searches"], 4)
        self.assertNotIn("can mice climb stairs", [q["query"] for q in x["queries"]])

    def test_too_few_searches_or_too_few_impressions_are_not_listed(self):
        self.assertEqual(op.long_tail(CLIMB[:2], CURVES, set()), [])                       # two searches are not a topic
        tiny = [row("can mice climb", 3, 6.0), row("can a mouse climb a wall", 2, 7.0), row("can mice climb up walls", 2, 8.0)]
        self.assertEqual(op.long_tail(tiny, CURVES, set()), [])                            # 7 impressions together

    def test_searches_at_the_top_or_far_down_do_not_count(self):
        rows = [row("can mice climb", 20, 2.0), row("can a mouse climb a wall", 12, 3.0), row("can mice climb up walls", 9, 3.4),
                row("can mice climb walls vertically", 9, 31.0), row("can mice climb a wall", 9, 45.0), row("can mice climb", 9, 60.0)]
        self.assertEqual(op.long_tail(rows, CURVES, set()), [])

    def test_two_topics_on_one_page_are_two_items(self):
        eat = [row("do mice eat peanuts", 14, 6.0), row("do mice eat peanut butter", 11, 7.0), row("what peanuts do mice eat", 8, 8.0)]
        found = op.long_tail(CLIMB + eat, CURVES, set())
        self.assertEqual(sorted(x["query"] for x in found), ["can mice climb", "do mice eat peanuts"])
        for x in found:
            words = " ".join(q["query"] for q in x["queries"])
            self.assertFalse("climb" in words and "peanut" in words)

    def test_the_sites_own_name_and_number_only_searches_are_skipped(self):
        rows = [row("micegone climb guide", 20, 6.0), row("micegone can mice climb", 12, 7.0), row("micegone climb walls", 9, 8.0),
                row("17-8", 20, 6.0), row("2+2", 12, 7.0), row("9 9 9", 9, 8.0)]
        self.assertEqual(op.long_tail(rows, CURVES, {"micegone"}), [])

    def test_words_the_searchers_keep_using_that_the_title_never_says_are_named(self):
        rows = [row("lucky number seven", 21, 6.0), row("is 7 a lucky number", 14, 8.8), row("lucky number 7 meaning", 4, 4.8),
                row("seven lucky number", 4, 7.0)]
        for r in rows:
            r["title"], r["h1"] = "The Power and Symbolism of Number Seven", "Number Seven"
        x = op.long_tail(rows, CURVES, set())[0]
        self.assertEqual(x["missing"], ["lucky"])
        self.assertIn("“lucky”", x["advice"])
        covered = op.long_tail(CLIMB, CURVES, set())[0]
        self.assertEqual(covered["missing"], [])
        self.assertNotIn("never do", covered["advice"])

    def test_the_same_data_in_any_order_gives_the_same_items(self):
        eat = [row("do mice eat peanuts", 14, 6.0), row("do mice eat peanut butter", 11, 7.0), row("what peanuts do mice eat", 8, 8.0)]
        rows = CLIMB + eat + [row("can mice climb", 15, 9.0, src="google"), row("can a mouse climb", 9, 9.0, src="google"),
                              row("mouse climb wall", 9, 10.0, src="google")]
        first = op.long_tail(rows, CURVES, set())
        rng = random.Random(3)
        for _ in range(6):
            mixed = rows[:]
            rng.shuffle(mixed)
            self.assertEqual(op.long_tail(mixed, CURVES, set()), first)

    def test_an_engine_without_a_curve_is_skipped(self):
        self.assertEqual(op.long_tail(CLIMB, {"google": CURVE}, set()), [])


class InTheSiteAnalysis(unittest.TestCase):
    def test_analyze_lists_and_counts_them_with_the_other_kinds(self):
        db = sqlite3.connect(":memory:")
        db.row_factory = sqlite3.Row
        db.executescript(store.SCHEMA)
        for migration in store.MIGRATIONS:
            try:
                db.execute(migration)
            except sqlite3.OperationalError:
                pass
        db.execute("INSERT INTO urls(url, site) VALUES (?, ?)", (URL, SITE))
        db.execute("INSERT INTO audits(url, status, title, h1) VALUES (?, 200, ?, ?)", (URL, "Can Mice Climb Walls?", "Can mice climb walls?"))
        # Clicks at the top give the site a click-through curve; without one every estimate is zero.
        top = [row("mice", 1000, 1.0, clicks=300), row("mouse", 1000, 2.0, clicks=150), row("mice guide", 1000, 3.0, clicks=100)]
        for r in top + CLIMB + [row("mice in walls", 200, 9.0)]:
            db.execute("INSERT INTO page_queries(src, url, site, query, clicks, impr, pos) VALUES (?,?,?,?,?,?,?)",
                       (r["src"], URL, SITE, r["query"], r["clicks"], r["impr"], r["pos"]))
        res = op.analyze(db, SITE, {URL: ("INDEXED", set())})
        kinds = [x["kind"] for x in res["items"]]
        self.assertEqual(sorted(kinds), ["close_to_top", "long_tail"])
        self.assertEqual((res["totals"]["long_tail"], res["totals"]["long_tail_searches"], res["totals"]["close_to_top"]), (1, 4, 1))
        self.assertEqual(res["totals"]["searches"], 5)
        self.assertAlmostEqual(res["totals"]["gain"], sum(x["gain"] for x in res["items"]), places=1)
        gains = [x["gain"] for x in res["items"]]
        self.assertEqual(gains, sorted(gains, reverse=True))           # one list, biggest estimated gain first
        self.assertEqual(op.site_summary(res)["long_tail"], 1)


if __name__ == "__main__":
    unittest.main()
