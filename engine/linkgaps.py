# -*- coding: utf-8 -*-
"""Link gaps: pages Google has not indexed that no trusted page links to from its text, and where a link could come from.

A page Google does not index, and that none of the site's indexed pages mentions in its text, has no route by which an
engine would reach or value it; a sitemap entry alone rarely gets such a page indexed. For each of these pages this
names up to three indexed (or Bing-earning) pages on the same subject that do not link to it yet.

It works for every site, WordPress or not: it reads the links the audit already recorded and fetches nothing. It
changes nothing either. On WordPress sites the link autopilot (engine/links.py) goes further and proposes the exact
words to link; this list is the wider net, and the only one for sites the engine cannot edit.

"On the same subject" is judged by the pages' names (title and main heading) and, on the sites whose posts the engine
keeps, by what the posts say as well (engine/related.py): a page that covers the subject under a differently worded
title is found, and of several pages sharing a title word the one that really is about it comes first.
"""
from collections import defaultdict
from urllib.parse import urlsplit

from . import links, related
from .triage import BLOCKERS

WANTED = ("UNKNOWN_TO_GOOGLE", "DISCOVERED_NOT_INDEXED", "CRAWLED_NOT_INDEXED")
TOP_ITEMS = 60              # pages listed per site, most valuable first
MAX_SOURCES = 3             # suggested linking pages per target
MAX_PER_SOURCE = 4          # times one page is suggested as a source within a site's list (a page cannot link to everything)
MIN_RELATED = 0.15          # a suggested page must share this much of the target's subject (links.relatedness)
# Where the posts' text is known, a suggested page must be one of the NEAR trusted pages closest to the target in
# what it says, and closer than nine in ten pairs of the site's pages are (related.Index.high). A shared title word is
# then no longer enough: "trainer" tied a page about waist trainers to three running-shoe reviews. The mixed score
# (related.blend) must also reach MIN_RELATED_TEXT: below it, the suggestions read on 26 Nov 2026 were mostly pages that
# share a template or a turn of phrase, not a subject.
NEAR = 20
MIN_RELATED_TEXT = 0.25
# A word says what a page is about only while few pages have it: at most a quarter of the site's pages (three pages
# on a very small site), and never more than this many. On a large generated site, parts of a reference number ("ocds", "h6vhtk") sit in hundreds of
# titles and would otherwise make unrelated pages look related.
SUBJECT_MAX_PAGES = 40
# On a site of this many pages or more, one shared word is chance ("realistic", "city"); two are a shared subject.
LARGE_SITE = 300


def _trusted(bucket, flags):
    return bucket not in BLOCKERS and "IGNORED" not in flags and (bucket == "INDEXED" or "BING_EARNER" in flags)


def analyze(db, site, buckets):
    """{"pages", "orphans", "trusted", "items"} for one site, or None when it has no trusted page to link from.
    pages    unindexed pages that no trusted page links to from its text
    orphans  ... of which no audited page of the site links to at all
    items    the TOP_ITEMS most valuable, each with the pages that could link to it"""
    trusted = {u for u, (b, f) in buckets.items() if _trusted(b, f)}
    if not trusted:
        return None
    info = {r["url"]: r for r in db.execute(
        """SELECT t.url, t.value, u.kind, a.title, a.h1 FROM triage t JOIN urls u ON u.url=t.url
           LEFT JOIN audits a ON a.url=t.url WHERE t.site=?""", (site,))}
    wanted = [u for u, (b, f) in buckets.items() if b in WANTED and "IGNORED" not in f and u in info
              and info[u]["kind"] != "taxonomy"
              and not links.UTILITY.search(([x for x in urlsplit(u).path.split("/") if x] or [""])[-1])]
    if not wanted:
        return {"pages": 0, "orphans": 0, "trusted": len(trusted), "items": []}
    inbound, in_text = defaultdict(set), defaultdict(set)      # target -> pages linking to it; ... from their article text
    for r in db.execute("SELECT l.src, l.dst, l.in_content FROM links l JOIN urls u ON u.url=l.src WHERE u.site=?", (site,)):
        if r["src"] != r["dst"] and r["src"] in buckets:
            inbound[r["dst"]].add(r["src"])
            if r["in_content"]:
                in_text[r["dst"]].add(r["src"])
    gaps = [u for u in wanted if not (in_text[u] & trusted)]
    gaps.sort(key=lambda u: (-(info[u]["value"] or 0), u))
    out = {"pages": len(gaps), "orphans": sum(1 for u in gaps if not inbound[u]), "trusted": len(trusted), "items": []}
    if not gaps:
        return out
    names = {u: links.page_names(u, info[u]["title"] if u in info else "", info[u]["h1"] if u in info else None) for u in buckets}
    df = defaultdict(int)
    for chunks, _slug in names.values():
        for w in {w for c in chunks for w in c}:
            df[w] += 1
    common = {w for w, k in df.items() if k > min(max(3, 0.25 * len(buckets)), SUBJECT_MAX_PAGES)}
    topic = {u: {w for c in chunks for w in c if links._topical(w) and w not in common} for u, (chunks, _slug) in names.items()}
    by_word = defaultdict(set)                                  # topic word -> trusted pages that have it
    for u in trusted:
        for w in topic.get(u, ()):
            by_word[w].add(u)
    suggested = defaultdict(int)
    need = 2 if len(buckets) >= LARGE_SITE else 1
    text = related.site_index(db, site)
    for t in gaps[:TOP_ITEMS]:
        mine = topic.get(t, set())
        scored = []
        read = text is not None and t in text            # the target's text is known: compare what the pages say
        if read:
            for alike, s in text.nearest(t, among=trusted, k=NEAR):
                rel = related.blend(links.relatedness(mine, topic[s], df, len(buckets)), alike)
                if alike > text.high() and rel >= MIN_RELATED_TEXT and s not in inbound[t]:
                    scored.append((-rel, s))
        else:
            for s in {s for w in mine for s in by_word.get(w, ())} - inbound[t] - {t}:
                rel = links.relatedness(mine, topic[s], df, len(buckets))
                if rel >= MIN_RELATED and len(mine & topic[s]) >= need:
                    scored.append((-rel, s))
        sources = []
        for neg, s in sorted(scored):
            if len(sources) >= MAX_SOURCES:
                break
            if suggested[s] >= MAX_PER_SOURCE:
                continue
            suggested[s] += 1
            shared = text.shared(t, s) if read else sorted(mine & topic[s], key=lambda w: (df.get(w, 0), w))[:3]
            sources.append({"url": s, "title": (info[s]["title"] if s in info else "") or "", "shared": shared,
                            "related": round(-neg, 2)})
        out["items"].append({"url": t, "title": (info[t]["title"] or "") if t in info else "", "bucket": buckets[t][0],
                             "inlinks": len(inbound[t]), "value": round(info[t]["value"] or 0, 3), "sources": sources})
    return out


def site_summary(gaps):
    """The compact per-site view for the dashboard summary."""
    if not gaps:
        return None
    return {"pages": gaps["pages"], "orphans": gaps["orphans"], "trusted": gaps["trusted"],
            "with_sources": sum(1 for x in gaps["items"] if x["sources"])}
