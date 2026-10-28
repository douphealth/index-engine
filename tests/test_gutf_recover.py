"""The owner-run gearuptofit cleanup script: exact edits, drift guards, backup, canary, read-back, rollback."""
import importlib.util
import json
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

SCRIPT = Path(__file__).resolve().parent.parent / "fixes" / "gutf_recover_20261028.py"
spec = importlib.util.spec_from_file_location("gutf_recover", SCRIPT)
rec = importlib.util.module_from_spec(spec)
spec.loader.exec_module(rec)

SCHEMA = ('<script type="application/ld+json">\n{\n  "@context": "https://schema.org",\n  "@graph": [\n    {"@type": "NewsArticle", "headline": "H",\n'
          '     "image": [{"@type": "ImageObject", "url": "data:image/jpeg;base64,' + "A" * 5000 + '"},\n'
          '               {"@type": "ImageObject", "url": "data:image/jpeg;base64,' + "B" * 5000 + '"}]},\n'
          '    {"@type": "Organization", "logo": {"@type": "ImageObject", "url": "https://x.test/logo.png"}}\n  ]\n}\n</script>')
QUOTE = ('<!-- wp:quote -->\n<blockquote class="wp-block-quote"><!-- wp:paragraph -->\n<p>“Iron is like fire.” – Dr. Lena Cho, 2024</p>\n'
         '<!-- /wp:paragraph -->\n\n</blockquote>\n<!-- /wp:quote -->\n\n')
RAW = ("<p>Intro. We tested 150 miles. Fresh insights here.</p>\n" + QUOTE + "<p>Avoid the guesswork.</p>\n<ul><li>keep</li>"
       "<li>By John Doe, NASM-certified trainer.</li></ul>\n" + SCHEMA + "\n<p>End.</p>")
EDITS = [("cut", " We tested 150 miles. Fresh insights here."), ("quote", "Dr. Lena Cho", 1),
         ("cut", "<li>By John Doe, NASM-certified trainer.</li>"), ("schema",)]
PAGE = dict(batch=1, id=7, path="/p/", why="t", edits=EDITS, gone=["We tested 150 miles", "Lena Cho"], schema=True)


class FakeWP:
    """mangle=1: WordPress alters the first save only; mangle=2: it alters every save (so a restore fails too)."""

    def __init__(self, posts, mangle=0):
        self.posts, self.mangle, self.writes = dict(posts), mangle, []

    def raw(self, pid, typ):
        return self.posts[pid], "2026-10-28T00:00:00"

    def update(self, pid, typ, text):
        self.writes.append(pid)
        altered = self.mangle == 2 or (self.mangle == 1 and len(self.writes) == 1)
        self.posts[pid] = text.replace("Avoid", "AVOID") if altered else text


class Env:
    """Redirect every side effect of the script into a temp folder and stubs."""

    def __init__(self, guard=((0, set()), (0, set())), status=200):
        self.tmp = tempfile.TemporaryDirectory()
        t = Path(self.tmp.name)
        (t / "CHANGELOG-AGENTS.md").write_text("# log\n\nRead this before you start: it is how the next agent learns what the last one did.\n\n## 2026-10-26 old\n", encoding="utf-8")
        resp = types.SimpleNamespace(status_code=status, text="<p>clean</p>", content=b"x" * 10, headers={})
        self.patches = [patch.object(rec, "BACKUP_DIR", t / "bk"), patch.object(rec, "CHANGELOG", t / "CHANGELOG-AGENTS.md"),
                        patch.object(rec, "guard_run", side_effect=list(guard) * 3), patch.object(rec, "served", return_value=resp),
                        patch.object(rec.time, "sleep"), patch.object(rec, "PAGES", [PAGE])]
        self.t = t

    def __enter__(self):
        for p in self.patches:
            p.start()
        return self

    def __exit__(self, *a):
        for p in reversed(self.patches):
            p.stop()
        self.tmp.cleanup()


ARGS = types.SimpleNamespace(batch=1, canary_only=False)


class EditTests(unittest.TestCase):
    def test_edits_remove_only_the_listed_text(self):
        new, notes, _ = rec.transform(RAW, EDITS)
        self.assertEqual(rec.check_result(PAGE, RAW, new), [])
        for gone in ("We tested", "Lena Cho", "John Doe", "data:image"):
            self.assertNotIn(gone, new)
        for kept in ("<p>Intro.</p>", "<p>Avoid the guesswork.</p>", "<li>keep</li>", "https://x.test/logo.png", '"headline": "H"', "<p>End.</p>"):
            self.assertIn(kept, new)
        self.assertEqual(len(notes), 4)

    def test_schema_stays_valid_json_and_keeps_other_nodes(self):
        new, _, _ = rec.transform(RAW, [("schema",)])
        body = rec.LD_BLOCK.search(new).group(2)
        graph = json.loads(body)["@graph"]
        self.assertEqual(graph[0]["headline"], "H")
        self.assertNotIn("image", graph[0])                      # every image was a data: URI, so the empty list goes
        self.assertEqual(graph[1]["logo"]["url"], "https://x.test/logo.png")

    def test_a_real_image_url_next_to_a_data_uri_is_kept(self):
        raw = ('<script type="application/ld+json">{"image":[{"@type":"ImageObject","url":"https://x.test/a.jpg"},'
               '{"@type":"ImageObject","url":"data:image/png;base64,AAAA"}]}</script>')
        new, _, _ = rec.transform(raw, [("schema",)])
        self.assertEqual(json.loads(rec.LD_BLOCK.search(new).group(2)), {"image": [{"@type": "ImageObject", "url": "https://x.test/a.jpg"}]})

    def test_drift_is_refused_never_guessed(self):
        with self.assertRaises(rec.Drift):
            rec.cut_text(RAW + RAW, " We tested 150 miles. Fresh insights here.")        # twice on the page
        with self.assertRaises(rec.Drift):
            rec.cut_text("<p>nothing</p>", "We tested")                                  # gone
        with self.assertRaises(rec.Drift):
            rec.cut_quote(RAW, "Dr. Lena Cho", 2)                                        # count differs from the review
        with self.assertRaises(rec.Drift):
            rec.cut_quote(RAW, "Dr. Nobody", 1)

    def test_quote_cut_stays_inside_one_blockquote(self):
        raw = "<blockquote><p>keep this</p></blockquote>\n<p>mid</p>\n<blockquote><p>x — Dr. A B</p></blockquote>\n<p>end</p>"
        new, removed, = rec.cut_quote(raw, "Dr. A B", 1)
        self.assertIn("keep this", new)
        self.assertNotIn("Dr. A B", new)
        self.assertEqual(len(removed), 1)

    def test_broken_figures_are_removed_and_decoded(self):
        raw = ('<p>a</p>\n<figure class="wp-block-image size-large"><img decoding="async" title="t" src="image/jpeg;base64,/9j/AAAA" alt="x">'
               '<figcaption>x</figcaption></figure>\n<p>b</p>')
        new, notes, images = rec.strip_broken_figures(raw, 1)
        self.assertEqual(new, "<p>a</p>\n<p>b</p>")
        self.assertEqual(images, [b"\xff\xd8\xff\x00\x00\x00"])
        with self.assertRaises(rec.Drift):
            rec.strip_broken_figures(raw, 2)

    def test_structured_edits_are_repeatable_but_text_cuts_are_not(self):
        first, _, _ = rec.transform(RAW, EDITS)
        again, notes, _ = rec.transform(first, [("quote", "Dr. Lena Cho", 1), ("schema",)])        # already gone: nothing to do
        self.assertEqual(again, first)
        self.assertTrue(all(n.startswith("already done") for n in notes))
        with self.assertRaises(rec.Drift):
            rec.transform(first, [("cut", " We tested 150 miles. Fresh insights here.")])         # a cut that finds nothing is refused
        mention = "<p>As Dr. Lena Cho says</p>"                                                    # named, but not inside a blockquote
        with self.assertRaises(rec.Drift):
            rec.transform(mention, [("quote", "Dr. Lena Cho", 1)])

    def test_sub_is_strict_once_and_repeatable(self):
        raw = "<p>I tell my clients to stretch.</p>"
        new, notes, _ = rec.transform(raw, [("sub", "I tell my clients to stretch.", "Stretch.")])
        self.assertEqual(new, "<p>Stretch.</p>")
        again, notes, _ = rec.transform(new, [("sub", "I tell my clients to stretch.", "Stretch.")])
        self.assertEqual(again, new)
        self.assertTrue(notes[0].startswith("already done"))
        with self.assertRaises(rec.Drift):
            rec.transform("<p>other</p>", [("sub", "I tell my clients to stretch.", "Stretch.")])
        with self.assertRaises(rec.Drift):
            rec.transform("<p>a a</p>", [("sub", "a", "b")])                                        # two copies: refused

    def test_a_quote_named_by_raw_entity_text_is_cut_not_skipped(self):
        raw = "<blockquote><p>&#8220;In my practice, I&#8217;ve observed x&#8221;</p></blockquote>\n<p>keep</p>"
        new, _, _ = rec.transform(raw, [("quote", "In my practice, I&#8217;ve observed", 1)])
        self.assertEqual(new, "<p>keep</p>")

    def test_a_page_that_would_still_carry_a_claim_is_a_problem(self):
        bad = dict(PAGE, edits=[("cut", "<li>By John Doe, NASM-certified trainer.</li>")])
        new, _, _ = rec.transform(RAW, bad["edits"])
        self.assertTrue(any("trust scan" in p for p in rec.check_result(bad, RAW, new)))


class ApplyTests(unittest.TestCase):
    def test_apply_backs_up_first_then_writes_logs_and_rollback_restores(self):
        with Env() as env:
            wpc = FakeWP({7: RAW})
            plans = rec.plan_pages(wpc, 1, None)
            self.assertEqual(len(plans), 1)
            self.assertEqual(wpc.writes, [])                                  # planning (the dry run) writes nothing
            rec.apply_batch(wpc, plans, ARGS)
            self.assertNotIn("Lena Cho", wpc.posts[7])
            bk = next((env.t / "bk").glob("backup-batch1-*.json"))
            saved = json.loads(bk.read_text(encoding="utf-8"))
            self.assertEqual(saved["pages"][0]["raw_before"], RAW)
            self.assertIn("gutf_recover_20261028.py batch 1", (env.t / "CHANGELOG-AGENTS.md").read_text(encoding="utf-8"))
            self.assertEqual(rec.rollback(wpc, bk), 0)
            self.assertEqual(wpc.posts[7], RAW)

    def test_rollback_will_not_overwrite_a_page_edited_since(self):
        with Env() as env:
            wpc = FakeWP({7: RAW})
            rec.apply_batch(wpc, rec.plan_pages(wpc, 1, None), ARGS)
            bk = next((env.t / "bk").glob("backup-batch1-*.json"))
            wpc.posts[7] += "<p>edited later</p>"
            self.assertEqual(rec.rollback(wpc, bk), 1)
            self.assertIn("edited later", wpc.posts[7])
            self.assertEqual(rec.rollback(wpc, bk, force=True), 0)
            self.assertEqual(wpc.posts[7], RAW)

    def test_wordpress_not_storing_the_intended_text_puts_the_page_back(self):
        with Env():
            wpc = FakeWP({7: RAW}, mangle=1)
            with self.assertRaises(SystemExit) as cm:
                rec.apply_batch(wpc, rec.plan_pages(wpc, 1, None), ARGS)
            self.assertEqual(wpc.posts[7], RAW)
            self.assertIn("was put back", str(cm.exception.code))

    def test_a_failed_restore_is_reported_not_claimed(self):
        with Env():
            wpc = FakeWP({7: RAW}, mangle=2)
            with self.assertRaises(SystemExit) as cm:
                rec.apply_batch(wpc, rec.plan_pages(wpc, 1, None), ARGS)
            self.assertIn("COULD NOT BE VERIFIED AS RESTORED", str(cm.exception.code))

    def test_a_page_that_errors_after_the_edit_is_put_back(self):
        with Env(status=500):
            wpc = FakeWP({7: RAW})
            with self.assertRaises(SystemExit):
                rec.apply_batch(wpc, rec.plan_pages(wpc, 1, None), ARGS)
            self.assertEqual(wpc.posts[7], RAW)

    def test_a_new_site_guard_block_rolls_the_batch_back(self):
        with Env(guard=((0, set()), (2, {"BLOCK sitemap"}))):
            wpc = FakeWP({7: RAW})
            with self.assertRaises(SystemExit) as cm:
                rec.apply_batch(wpc, rec.plan_pages(wpc, 1, None), ARGS)
            self.assertEqual(cm.exception.code, 2)
            self.assertEqual(wpc.posts[7], RAW)

    def test_a_rerun_after_the_canary_treats_cleaned_pages_as_done(self):
        with Env():
            wpc = FakeWP({7: RAW})
            rec.apply_batch(wpc, rec.plan_pages(wpc, 1, None), ARGS)
            writes = len(wpc.writes)
            self.assertEqual(rec.plan_pages(wpc, 1, None), [])               # "already clean", not "changed since"
            self.assertEqual(len(wpc.writes), writes)

    def test_drifted_pages_are_skipped_not_written(self):
        with Env():
            wpc = FakeWP({7: RAW.replace("We tested 150 miles.", "We checked.")})
            self.assertEqual(rec.plan_pages(wpc, 1, None), [])
            self.assertEqual(wpc.writes, [])

    def test_verify_counts_pages_that_still_show_the_old_text(self):
        def fake(path, plain=False):
            text = "<p>clean</p>" if not plain else "<p>We tested 150 miles</p>"
            return types.SimpleNamespace(status_code=200, text=text, content=text.encode(), headers={"cf-cache-status": "HIT", "age": "600"})
        with Env(), patch.object(rec, "served", side_effect=fake):
            self.assertEqual(rec.verify_live(1, None), 0)          # the ?nc= copy is clean; the plain URL is still the old cached copy
        with Env(), patch.object(rec, "served", return_value=types.SimpleNamespace(
                status_code=200, text="<p>We tested 150 miles</p>", content=b"x", headers={})):
            self.assertEqual(rec.verify_live(1, None), 1)

    def test_verify_ignores_the_themes_tiny_inline_images_on_the_plain_url(self):
        import contextlib
        import io
        tiny = '<img src="data:image/gif;base64,R0lGODlhAQABAAAAACw=">'
        big = '<img src="data:image/jpeg;base64,' + "A" * 2500 + '">'

        def fake(path, plain=False):
            text = ("<p>clean</p>" + tiny) if not plain else ("<p>clean</p>" + (big if path == "/old/" else tiny))
            return types.SimpleNamespace(status_code=200, text=text, content=text.encode(), headers={"cf-cache-status": "HIT"})
        pages = [dict(PAGE, id=1, path="/new/"), dict(PAGE, id=2, path="/old/")]
        buf = io.StringIO()
        with Env(), patch.object(rec, "PAGES", pages), patch.object(rec, "served", side_effect=fake), contextlib.redirect_stdout(buf):
            self.assertEqual(rec.verify_live(1, None), 0)
        out = buf.getvalue()
        self.assertIn("cleaned 200", out)
        self.assertIn("old cached copy 200", out)
        self.assertIn("1 of 2 already show the cleaned page", out)

    def test_real_page_list_respects_the_batch_limits(self):
        for batch in (1, 2):
            self.assertLessEqual(sum(1 for p in rec.PAGES if p["batch"] == batch), rec.MAX_PER_BATCH)
        self.assertEqual(len({p["id"] for p in rec.PAGES}), len(rec.PAGES))


if __name__ == "__main__":
    unittest.main()
