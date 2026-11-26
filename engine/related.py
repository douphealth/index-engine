# -*- coding: utf-8 -*-
"""How much two pages are about the same subject, read from what they say.

links.relatedness() compares the topic words of two pages' names (title and main heading). That misses pages that
cover the same subject under differently worded titles, and it cannot rank the many pages that share one common title
word. This module compares the words of the article text instead: each page is its words weighted by how often it
uses them and how few other pages do (TF-IDF), and two pages are as related as their weighted words overlap (cosine).

Measured on 26 Nov 2026 against the 38,145 in-article links editors had placed between posts on the eight WordPress
sites (never links the engine applied; the words of the links themselves were left out of the text). Asked which pages
link to a page, the name comparison alone put a real linking page in its first three guesses 20.2% of the time, and
blend() of the two 25.9%; the mean average precision rose from 0.151 to 0.200.

The weighting is scikit-learn's TfidfVectorizer(sublinear_tf=True, min_df=2, max_df=0.5): term weight
(1 + ln count) * (ln((1 + pages) / (1 + pages with the term)) + 1), each page scaled to unit length. It is written out
here, in about thirty lines, so the engine keeps its three runtime dependencies; tests/test_related.py checks every
number against scikit-learn itself wherever that is installed (CI installs it).
"""
import html
import math
import random
import re
from collections import Counter, defaultdict

from . import links, memo

TERMS_VERSION = "terms-1"   # change it whenever page_terms() or links.tokens()/stem() change: cached counts are redone
MAX_TERMS = 200             # a page's most used words are kept; the long tail adds size, not signal (ranking unchanged)
MIN_DF = 2                  # a word only one page uses cannot relate two pages
MAX_DF = 0.5                # a word on more than half of a site's pages is the site's vocabulary, not a subject
MIN_PAGES = 8               # below this a site has too few pages for word rarity to mean anything
# blend(): the share of the name comparison; the rest is the text. 0.3 ranked the real links best of 0.3, 0.5 and 0.7.
NAME_SHARE = 0.3
# The mixed score that counts as fully related (1.0), so that blend() reads on the same 0..1 scale as
# links.relatedness() alone. Dividing by it does not change which page ranks first.
FULL = 0.5
# A text similarity this high is said to the owner as "covers the same subject": of the page pairs editors linked, only
# one in ten reaches it.
SAME_SUBJECT = 0.25

_DROP = re.compile(r"<(script|style)\b.*?</\1\s*>|<!--.*?-->", re.S | re.I)
_TAG = re.compile(r"<[^>]+>|\[/?[A-Za-z][\w-]*[^\]\n]{0,300}\]")


def page_terms(raw):
    """{stemmed word: times used} for the visible text of one post, its MAX_TERMS most used words (ties by spelling).
    Depends only on the content, so engine/memo.py keeps it until the post changes."""
    text = html.unescape(_TAG.sub(" ", _DROP.sub(" ", raw)))
    counts = Counter(t for t, _, _ in links.tokens(text) if links._topical(t))
    return dict(sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[:MAX_TERMS])


class Index:
    """The pages of one site as unit-length TF-IDF vectors ({word: weight}), from {url: {word: count}}."""

    def __init__(self, counts):
        n = len(counts)
        df = Counter(t for c in counts.values() for t in c)
        idf = {t: math.log((1 + n) / (1 + k)) + 1 for t, k in df.items() if MIN_DF <= k <= MAX_DF * n}
        self.vec = {}
        for url, c in counts.items():
            w = {t: (1 + math.log(k)) * idf[t] for t, k in c.items() if t in idf and k > 0}
            norm = math.sqrt(sum(x * x for x in w.values()))
            self.vec[url] = {t: x / norm for t, x in w.items()} if norm else {}
        self._postings = self._high = None

    def __contains__(self, url):
        return bool(self.vec.get(url))

    def cosine(self, a, b):
        """0..1: how much of their weighted vocabulary pages a and b share (0 when either is unknown)."""
        va, vb = self.vec.get(a), self.vec.get(b)
        if not va or not vb:
            return 0.0
        if len(vb) < len(va):
            va, vb = vb, va
        return sum(x * vb[t] for t, x in va.items() if t in vb)

    def nearest(self, a, among=None, k=20):
        """[(cosine, url)] of the k pages most like a, best first; only pages in `among` when it is given."""
        va = self.vec.get(a)
        if not va:
            return []
        if self._postings is None:
            self._postings = defaultdict(list)
            for url, v in self.vec.items():
                for t, x in v.items():
                    self._postings[t].append((url, x))
        score = defaultdict(float)
        for t, x in va.items():
            for url, y in self._postings[t]:
                score[url] += x * y
        score.pop(a, None)
        found = [(s, u) for u, s in score.items() if among is None or u in among]
        return sorted(found, key=lambda su: (-su[0], su[1]))[:k]

    def high(self):
        """The text similarity that only one pair of this site's pages in ten exceeds. How alike two pages read
        depends on how a site writes (lists of prompts overlap less than long reviews), so "close" is judged against
        the site's own pairs, from a fixed sample of them: the same pages give the same number on every run."""
        if self._high is None:
            urls = sorted(u for u, v in self.vec.items() if v)
            rng, seen = random.Random(len(urls)), []
            if len(urls) >= 2:
                for _ in range(min(3000, len(urls) * (len(urls) - 1) // 2)):
                    a, b = rng.sample(urls, 2)
                    seen.append(self.cosine(a, b))
            seen.sort()
            self._high = seen[int(0.9 * len(seen))] if seen else 0.0
        return self._high

    def shared(self, a, b, k=3):
        """The k words that tie pages a and b together most (largest product of their weights)."""
        va, vb = self.vec.get(a) or {}, self.vec.get(b) or {}
        both = sorted(((va[t] * vb[t], t) for t in va if t in vb), key=lambda xt: (-xt[0], xt[1]))
        return [t for _, t in both[:k]]


def site_index(db, site):
    """The Index of a site's stored posts (WordPress sites, whose content the engine keeps), or None when the site has
    no stored posts or too few for it to mean anything. Counting a post's words is cached until the post changes."""
    counts = memo.per_post(db, site, "terms", TERMS_VERSION, page_terms)
    return Index(counts) if len(counts) >= MIN_PAGES else None


def blend(name_score, text_score):
    """One 0..1 score from the name comparison (links.relatedness) and the text comparison (Index.cosine)."""
    return min(1.0, (NAME_SHARE * name_score + (1 - NAME_SHARE) * text_score) / FULL)
