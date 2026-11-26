"""Rules that must hold for any input, checked on generated inputs (Hypothesis).

The link autopilot writes into live posts, so its rules are worth more than examples: wherever find() places a link,
no word of the post changes, no tag is cut, the link is never inside another link, a heading, a quote or a shortcode,
and the same post is never linked to the same page twice. Skipped where Hypothesis is not installed (CI installs it).
"""
import html
import importlib.util
import re
import unittest

from engine import links, related, util

if importlib.util.find_spec("hypothesis"):
    from hypothesis import given, settings, strategies as st
    HAVE = True
else:                                                    # keep the module importable: every test is skipped
    HAVE = False

    class _Anything:
        def __getattr__(self, name):
            return lambda *a, **k: _Anything()

        def __call__(self, *a, **k):
            return _Anything()

        def __or__(self, other):
            return self

    st = _Anything()

    def given(*a, **k):
        return lambda f: f

    def settings(*a, **k):
        return lambda f: f

TARGET = "https://example.test/trail-shoes/"
WORDS = ["trail", "shoes", "running", "Garmin", "watch", "battery", "runners", "marathon", "training", "plan", "the", "a",
         "of", "for", "with", "and", "is", "are", "when", "your", "best", "long", "easy", "weekly", "miles", "heart", "rate",
         "it's", "runner's", "10", "2026", "GPS"]
word = st.sampled_from(WORDS)
glue = st.sampled_from([" "] * 14 + ["&nbsp;", " &amp; ", ", ", " - ", "\n"])


@st.composite
def run_of_words(draw, low=3, high=16):
    parts = draw(st.lists(word, min_size=low, max_size=high))
    out = parts[0]
    for w in parts[1:]:
        out += draw(glue) + w
    return out


@st.composite
def inline(draw):
    text = draw(run_of_words())
    kind = draw(st.sampled_from(["plain"] * 8 + ["strong", "em", "a", "quote", "code", "span"]))
    return {"plain": text, "strong": f"<strong>{text}</strong>", "em": f"<em>{text}</em>",
            "a": f'<a href="https://example.test/other/">{text}</a>', "quote": f"“{text}”",
            "code": f"<code>{text}</code>", "span": f'<span class="x">{text}</span>'}[kind]


@st.composite
def sentence(draw):
    return "".join(p + " " for p in draw(st.lists(inline(), min_size=1, max_size=4))).rstrip() + draw(st.sampled_from([". ", "? ", ": ", "! ", " "]))


@st.composite
def block(draw):
    body = "".join(draw(st.lists(sentence(), min_size=1, max_size=4)))
    kind = draw(st.sampled_from(["p"] * 6 + ["h2", "li", "blockquote", "figure", "shortcode", "table", "comment", "bare"]))
    return {"p": f"<p>{body}</p>", "h2": f"<h2>{body}</h2>", "li": f"<ul><li>{body}</li><li>{body}</li></ul>",
            "blockquote": f"<blockquote><p>{body}</p></blockquote>",
            "figure": f'<figure><img src="x.jpg" alt="{html.escape(body[:30], quote=True)}"/><figcaption>{body}</figcaption></figure>',
            "shortcode": f'[caption id="a" width="300"]{body}[/caption]', "table": f"<table><tr><td>{body}</td></tr></table>",
            "comment": f"<!-- wp:paragraph -->\n<p>{body}</p>\n<!-- /wp:paragraph -->", "bare": body}[kind]


post = st.lists(block(), min_size=1, max_size=6).map("\n".join)
phrase = st.lists(word, min_size=2, max_size=3).map(" ".join)
TAG = re.compile(r"<[^>]*>")
INSIDE = re.compile(r"<(/?)(a|h[1-6]|blockquote|figcaption|code|table|figure)\b[^>]*>", re.I)


def words_of(raw):
    return re.sub(r"\s+", " ", html.unescape(TAG.sub("", raw)))


def open_at(raw, pos):
    """The tags among INSIDE that are open at this position of the post."""
    depth = {}
    for m in INSIDE.finditer(raw, 0, pos):
        name = m.group(2).lower()
        depth[name] = depth.get(name, 0) + (-1 if m.group(1) else 1)
    return {n for n, d in depth.items() if d > 0}


@unittest.skipUnless(HAVE, "Hypothesis is not installed here (CI installs it)")
class WhereALinkMayGo(unittest.TestCase):
    placed = 0

    @settings(max_examples=600, deadline=None, derandomize=True, database=None)
    @given(post, st.data())
    def test_a_link_changes_no_word_cuts_no_tag_and_is_never_nested_or_repeated(self, raw, data):
        said_in_post = re.findall(r"[A-Za-z0-9']+", words_of(raw))
        if len(said_in_post) >= 3:                       # mostly a phrase the post really says, so links do get placed
            i = data.draw(st.integers(0, len(said_in_post) - 2))
            said = " ".join(said_in_post[i:i + data.draw(st.integers(2, 3))])
        else:
            said = data.draw(phrase)
        hit = links.find(raw, said, TARGET)
        if hit is None:
            return
        type(self).placed += 1
        a, b, context = hit
        anchor = raw[a:b]
        self.assertEqual(anchor.lower().split(), said.lower().split())                 # the words asked for, nothing else
        self.assertNotRegex(anchor, r"[<>\[\]]")                                       # no markup inside the link
        self.assertTrue(any(ok and s <= a and b <= e for s, e, ok in links.segments(raw)))
        self.assertEqual(open_at(raw, a), set())                                       # not in a link, heading, quote, table...
        self.assertEqual(raw[:a].count("[caption"), raw[:a].count("[/caption]"))       # not inside a shortcode's content
        new = raw[:a] + f'<a href="{TARGET}">' + anchor + "</a>" + raw[b:]
        self.assertEqual(words_of(new), words_of(raw))                                 # not one word of the post changed
        self.assertTrue(links.links_to(new, TARGET))
        self.assertIsNone(links.find(new, said, TARGET))                               # never a second link to the same page
        self.assertIn(" ".join(said.lower().split()), re.sub(r"\s+", " ", context.lower()))

    @classmethod
    def tearDownClass(cls):
        assert cls.placed >= 50, f"only {cls.placed} generated cases placed a link: the rules above were hardly tested"

    @settings(max_examples=300, deadline=None, derandomize=True, database=None)
    @given(post)
    def test_text_runs_tile_the_post_and_tokens_point_at_their_words(self, raw):
        runs = links.segments(raw)
        for (s1, e1, _), (s2, _e2, _) in zip(runs, runs[1:]):
            self.assertLessEqual(e1, s2)                                               # in order, never overlapping
        for s, e, _ok in runs:
            self.assertNotRegex(raw[s:e], r"<[A-Za-z/!]")                              # a run of text holds no tag
            for stem, ts, te in links.tokens(raw[s:e]):
                self.assertTrue(0 <= ts < te <= e - s)
                self.assertEqual(stem, links.stem(raw[s:e][ts:te]))
        stream = links.stem_stream(raw)
        for s, e, text in stream:
            self.assertEqual(text, " ".join(t for t, _, _ in links.tokens(raw[s:e])))

    @settings(max_examples=200, deadline=None, derandomize=True, database=None)
    @given(post)
    def test_the_words_counted_for_a_page_are_words_it_shows(self, raw):
        shown = {t for t, _, _ in links.tokens(words_of(re.sub(r"\[/?caption[^\]]*\]", " ", raw)))}
        self.assertTrue(set(related.page_terms(raw)) <= shown)


host = st.sampled_from(["example.test", "Example.TEST", "www.example.test", "example.test:443", "example.test:80", "example.test:8080"])
path = st.lists(st.sampled_from(["a", "B", "trail-shoes", "x_y", "%20", "2026", ""]), max_size=4).map(lambda p: "/" + "/".join(p))
url = st.builds(lambda sch, h, p, q, f: f"{sch}://{h}{p}{q}{f}", st.sampled_from(["http", "https", "HTTPS", "ftp"]), host, path,
                st.sampled_from(["", "?utm_source=x", "?a=1&b=2"]), st.sampled_from(["", "#top", "#"]))


@unittest.skipUnless(HAVE, "Hypothesis is not installed here (CI installs it)")
class UrlForms(unittest.TestCase):
    @settings(max_examples=300, deadline=None, derandomize=True, database=None)
    @given(url)
    def test_the_canonical_form_is_stable_and_carries_no_query_or_fragment(self, u):
        n = util.norm(u)
        if not n:
            self.assertTrue(u.lower().startswith("ftp"))                               # only non-web URLs have none
            return
        self.assertEqual(util.norm(n), n)                                              # normalising twice changes nothing
        self.assertNotIn("?", n)
        self.assertNotIn("#", n)
        self.assertTrue(util.same_page(u, n))
        self.assertTrue(util.same_page(n, n.rstrip("/") + "/") or n.count("/") == 3)


if __name__ == "__main__":
    unittest.main()
