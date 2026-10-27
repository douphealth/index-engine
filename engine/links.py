# -*- coding: utf-8 -*-
"""Internal-link autopilot: propose contextual links with natural anchors, apply the approved ones, undo on request.

Links from pages Google already indexes are the strongest lever this portfolio has for "discovered / unknown /
crawled, not indexed" pages. Rules that keep it safe:
  * the anchor is a phrase that ALREADY exists in the source page's own text, taken from the target's title or URL
    slug, and no other page on the site has it in its title or slug (so the link text names where it leads);
    nothing is written but the <a> tag around it - no new sentences, blocks or boxes;
  * sources are only pages Google indexes or that earn Bing clicks; at most 3 new links per target and 2 per source
    per run;
  * never inside headings, links, buttons, captions, quotes, tables, code, scripts or shortcodes;
  * only approved links are applied (approval in the dashboard), at most `links_per_site_per_day` per site
    (default 5, never above 10); sites in STRICT_SITES are never auto-approved (gearuptofit AGENTS.md rule 3/2);
  * raw content is backed up before each save, the result is verified in WordPress and on the live page,
    the change is logged, and Undo removes exactly the link that was added.
"""
import datetime as dt
import hashlib
import html
import json
import math
import re
import subprocess
import sys
import time
import zlib
from collections import defaultdict
from pathlib import Path
from urllib.parse import urlsplit

import requests

from . import config, store
from .triage import BLOCKERS, STOP as _STOP, clean_title
from .util import SECRETS, bare_host, days_since, get, log, norm, now_iso

STRICT_SITES = {"gearuptofit.com"}
MAX_PER_DAY = 10
FORBIDDEN = {"a", "h1", "h2", "h3", "h4", "h5", "h6", "script", "style", "code", "pre", "button", "figcaption", "select",
             "textarea", "label", "summary", "title", "nav", "header", "footer", "aside", "figure", "table", "iframe",
             "noscript", "svg", "blockquote", "cite", "q", "option"}
VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}
TOKEN = re.compile(r"(<!--.*?-->|<![^>]*>|<[^>]+>|\[/?[A-Za-z][\w-]*[^\]\n]{0,300}\])", re.S)


def _backups():
    return config.OUT / "backups" / "links"


def segments(raw):
    """(start, end, safe) for every run of text in raw post content."""
    closing = set(m.group(1).lower() for m in re.finditer(r"\[/([A-Za-z][\w-]*)\]", raw))
    stack, pos, out = [], 0, []

    def safe():
        return not any(t in FORBIDDEN or t.startswith("sc:") for t in stack)

    for m in TOKEN.finditer(raw):
        if m.start() > pos:
            out.append((pos, m.start(), safe()))
        tok = m.group(0)
        if tok.startswith("[") and not tok.startswith("[/"):
            name = re.match(r"\[([A-Za-z][\w-]*)", tok).group(1).lower()
            if name in closing:
                stack.append("sc:" + name)
        elif tok.startswith("[/"):
            name = "sc:" + re.match(r"\[/([A-Za-z][\w-]*)", tok).group(1).lower()
            if name in stack:
                del stack[len(stack) - 1 - stack[::-1].index(name):]
        elif tok.startswith("<") and not tok.startswith("<!"):
            nm = re.match(r"</?\s*([A-Za-z][A-Za-z0-9]*)", tok)
            if nm:
                n = nm.group(1).lower()
                if tok.startswith("</"):
                    if n in stack:
                        del stack[len(stack) - 1 - stack[::-1].index(n):]
                elif not tok.rstrip().endswith("/>") and n not in VOID:
                    stack.append(n)
        pos = m.end()
    if pos < len(raw):
        out.append((pos, len(raw), safe()))
    return out


def links_to(html, target):
    """True when html contains an href to exactly this URL (with or without the trailing slash)."""
    return re.search(r"""href\s*=\s*["']""" + re.escape(target.rstrip("/")) + r"""/?["'#?]""", html or "") is not None


SENT_END = re.compile(r"[.!?:;](?:&#8221;|&#8217;|&rdquo;|&rsquo;|[\"'\u201d\u2019)\]])*\s+|\n")
MIN_SENTENCE_WORDS = 8      # an anchor sits inside a real sentence, not a label, caption or list entry


def natural(raw, s, e, a, b):
    """May the words raw[a:b] (inside the text run raw[s:e]) become a link? Not when the sentence around them is too
    short to be running text ("Fish Oil (Omega-3 Fatty Acids):"), is a lead-in ending in a colon, or when the words sit
    inside quotation marks (a quoted term or search query reads wrong as a link)."""
    before, after = raw[s:a], raw[b:e]
    start = max([m.end() for m in SENT_END.finditer(before)] or [0])
    m = SENT_END.search(after)
    end = m.start() + 1 if m else len(after)
    sentence = before[start:] + raw[a:b] + after[:end]
    if len(tokens(sentence)) < MIN_SENTENCE_WORDS or sentence.rstrip().endswith(":"):
        return False
    text = html.unescape(before[start:])
    return not (text.count("\u201c") > text.count("\u201d") or text.count('"') % 2 == 1)


def find(raw, phrase, target):
    """First safe, natural occurrence of phrase: (start, end, context) or None. Skips pages that already link to
    target. The context is plain text (HTML entities decoded)."""
    if links_to(raw, target):
        return None
    rx = re.compile(r"(?<![\w-])" + r"\s+".join(map(re.escape, phrase.split())) + r"(?![\w-])", re.I)
    for s, e, ok in segments(raw):
        if not ok:
            continue
        for m in rx.finditer(raw, s, e):
            if natural(raw, s, e, m.start(), m.end()):
                text = re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", raw[max(s, m.start() - 160):min(e, m.end() + 120)]))).strip()
                return m.start(), m.end(), text
    return None


def locate(db, wpc, url):
    """(post id, type) of the published post or page at this URL. The stored corpus knows every one of them, including
    the front page and pages whose permalink is not their slug; the slug lookup is the fallback."""
    row = db.execute("SELECT post_id, post_type FROM link_corpus WHERE url=?", (norm(url),)).fetchone()
    return (row["post_id"], row["post_type"]) if row else wpc.find(url)


def link_id(source, target):
    return hashlib.sha1(f"{source}|{target}".encode()).hexdigest()[:16]


# ---------------------------------------------------------------- finding natural anchors
# Every published post's stored content is kept locally (link_corpus, refreshed incrementally). For each page that
# needs links, its distinctive phrases (from its title, or a long one from its URL slug) are looked up in the article
# text of every page Google already indexes or that earns Bing clicks. A phrase qualifies only when it names that one
# page: no other title or slug on the site contains it, it is a whole phrase of the title (never a fragment across
# punctuation or a compound cut short), it carries the title's main topic, and it is not a stock phrase used all over
# the site's text.
YEAR = re.compile(r"^(19|20)\d\d$")
ENTITY = re.compile(r"&(?:#\d+|#x[0-9a-f]+|[a-z]+);", re.I)
WORD = re.compile(r"[A-Za-z0-9]+")
GAP = re.compile(r"^(?:\s|-|–|&nbsp;|&#160;|&#8211;|['’]s|&#8217;s|&#039;s|&#39;s)+$", re.I)   # allowed between anchor words
CHUNK = re.compile(r"\s*(?:[:|?!()\[\],;&—–]|\s-\s)\s*")           # title punctuation that ends a phrase
CORPUS_FULL_DAYS = 7
MAX_PER_TARGET = 3          # the model's "add links" estimate is for about 3 new links
MAX_PER_SOURCE = 2          # new links proposed into one page per run
MAX_PER_SITE = 60           # proposals per site per run (each one waits for the owner's approval)
STOCK_SHARE = 0.08          # a phrase found in the text of more than 8% of a site's pages is a stock phrase, not a name


def stem(w):
    """Light, predictable normalisation so 'Shoes' in a title matches 'shoe' in text (never over-merges)."""
    w = w.lower()
    if len(w) > 4 and w.endswith("ies"):
        return w[:-3] + "y"
    if len(w) > 4 and w.endswith(("sses", "xes", "ches", "shes")):
        return w[:-2]
    if len(w) > 3 and w.endswith("s") and not w.endswith(("ss", "us", "is")):
        return w[:-1]
    return w


STOP = _STOP | {stem(w) for w in _STOP}
# Words an anchor may not start or end on: function words, filler and headline adjectives.
EDGE = STOP | {stem(w) for w in """through between across after before during over under against among around within like
based using via per while still also just even only much many some such very same other each both own first last next
real way step thing help work start now then fast proven essential surprising amazing effective practical honest full
free expert simple smart powerful perfect great good better right common key important definitive near me understanding
unlock unlocking discover explore exploring mastering decode decoding power importance significance safely quickly
naturally easily set boost improve""".split()}
END_EDGE = EDGE | {stem(w) for w in "daily weekly monthly yearly today tonight often need".split()}
# What follows these in a title is what the page is NOT about ("Running Shoes Without a Carbon Plate").
NEGATION = {"without", "no", "not", "non", "never", "instead", "avoid"}
END_EDGE |= NEGATION
# A title word after the phrase that does not make the phrase a fragment ("habit tracker" in "Habit Tracker Apps").
# These category words never start an anchor ("tools for beginners" names no page).
TAIL_OK = {stem(w) for w in """app tool guide tip idea example review checklist plan list prompt question template
calculator chart meaning explained""".split()}
UTILITY = re.compile(r"(^|-)(disclosure|privacy|terms|cookie|cookies|contact|about|editorial|policy|disclaimer|"
                     r"sitemap|author|accessibility|imprint|legal)(-|$)")


def tokens(text):
    """[(stem, start, end)] of the words in a run of text; HTML entities count as gaps, a possessive 's is dropped."""
    masked = ENTITY.sub(lambda m: " " * len(m.group(0)), text)
    return [(stem(m.group(0)), m.start(), m.end()) for m in WORD.finditer(masked)
            if not (m.group(0).lower() == "s" and m.start() and masked[m.start() - 1] in " '’")]


def _topical(w):
    return w not in STOP and (len(w) > 2 or (w.isdigit() and len(w) > 1)) and not YEAR.match(w)


def title_words(url, title):
    """(title chunks split at punctuation, slug words), each a list of stems."""
    chunks = [[t for t, _, _ in tokens(c)] for c in CHUNK.split(clean_title(title or "")) if c.strip()]
    slug = [s for s in urlsplit(url).path.split("/") if s]
    return ([c for c in chunks if any(_topical(w) for w in c)],
            [t for t, _, _ in tokens(slug[-1].replace("-", " "))] if slug else [])


def all_grams(chunks, slug):
    """Every 2-5 word run of a page's title chunks and slug: what the page's name contains."""
    out = set()
    for words in chunks + [slug]:
        for n in (2, 3, 4, 5):
            out.update(tuple(words[i:i + n]) for i in range(len(words) - n + 1))
    return out


def title_phrases(chunks, common):
    """{key: from_main_chunk} for the WHOLE phrases a title offers as anchors. A run may drop leading words only when
    they are function or site-wide words ("French Bulldog Tail Pocket" -> "tail pocket", but "Rodent Control
    Strategies" -/-> "control strategies"), and trailing words only when they are function or category words
    ("Habit Tracker Apps" -> "habit tracker", but "Runner's Weight Loss" -/-> "runner weight"). Slugs only count
    against other pages (uniqueness), never as anchors: their runs cross what the title separates."""
    out = {}
    for ci, words in enumerate(chunks):
        for n in (5, 4, 3, 2):
            for i in range(len(words) - n + 1):
                key = tuple(words[i:i + n])
                before, after = words[i - 1] if i else None, words[i + n] if i + n < len(words) else None
                if key[0] in EDGE or key[0] in TAIL_OK or key[-1] in END_EDGE or NEGATION & set(words[:i]):
                    continue
                if (before and before not in STOP and before not in common) or (after and after not in STOP and after not in TAIL_OK):
                    continue
                out[key] = out.get(key, False) or ci == 0
    return out


def _usable(key, common, is_main, main_words, core):
    """At least two topic words, one of them neither a site-wide word nor a short number, and the title's subject:
    a phrase from the main part must contain its rarest word (the one that names this page: "Perplexity" in "How to
    Use Perplexity AI to Boost Your Affiliate Earnings"); one from a subtitle must share a word with the main part."""
    if any(YEAR.match(w) for w in key):
        return False
    topical = [w for w in key if _topical(w)]
    naming = [w for w in topical if w not in common and not (w.isdigit() and len(w) < 3)]
    if len(topical) < 2 or not naming:
        return False
    return bool(set(topical) & core) if is_main else bool(set(topical) & main_words)


def refresh_corpus(db, site, wpc):
    """Keep the stored content of every published post/page; only what changed since the last look is re-read."""
    meta = store.get_meta(db, site)
    age = days_since(meta.get("corpus_full_at"))
    full = age is None or age >= CORPUS_FULL_DAYS
    after = None if full else meta.get("corpus_watermark")
    n, newest = 0, meta.get("corpus_watermark") or ""
    if full:
        db.execute("DELETE FROM link_corpus WHERE site=?", (site,))
    for item in wpc.corpus(after=after):
        db.execute("INSERT OR REPLACE INTO link_corpus VALUES(?,?,?,?,?,?)",
                   (item["url"], site, item["id"], item["type"], item["modified"], zlib.compress(item["raw"].encode("utf-8"))))
        newest = max(newest, item["modified"] or "")
        n += 1
    store.set_meta(db, site, corpus_watermark=newest, **({"corpus_full_at": now_iso()} if full else {}))
    db.commit()
    return n


def corpus(db, site):
    return {r["url"]: zlib.decompress(r["raw"]).decode("utf-8") for r in db.execute(
        "SELECT url, raw FROM link_corpus WHERE site=?", (site,))}


def candidate_starts(keys):
    """Index possible first words by phrase length; reusable across a site's pages."""
    starts = {n: set() for n in (5, 4, 3, 2)}
    for key in keys:
        if len(key) in starts:
            starts[len(key)].add(key[0])
    return starts


def candidates(raw, keys, starts=None):
    """{key: (start, end)} for the first safe occurrence in raw of each phrase key."""
    found = {}
    if starts is None:
        starts = candidate_starts(keys)
    if not any(starts.values()):
        return found
    for s, e, ok in segments(raw):
        if not ok:
            continue
        toks = tokens(raw[s:e])
        for n in (5, 4, 3, 2):
            if not starts[n] or len(toks) < n + 3:       # inside a sentence, not a bare label or list entry
                continue
            for i in range(len(toks) - n + 1):
                if toks[i][0] not in starts[n]:
                    continue
                key = tuple(t for t, _, _ in toks[i:i + n])
                if key not in keys or key in found:
                    continue
                if all(GAP.match(raw[s + toks[j][2]:s + toks[j + 1][1]]) for j in range(i, i + n - 1)):
                    found[key] = (s + toks[i][1], s + toks[i + n - 1][2])
    return found


def propose(db, results, link_gain, clients, per_site=MAX_PER_SITE):
    """Natural-anchor proposals: for each page where links should help (best expected gain first), up to
    MAX_PER_TARGET links from different trusted pages whose article text already names it."""
    titles = {r["url"]: r["title"] or "" for r in db.execute("SELECT url, title FROM audits")}
    out = []
    for site, wpc in clients.items():
        res = results.get(site) or {}
        buckets, rows = res.get("buckets") or {}, {r["url"]: r for r in res.get("rows") or []}
        if not buckets or not getattr(wpc, "ok", False):
            continue
        try:
            changed = refresh_corpus(db, site, wpc)
        except Exception as e:
            log(f"[links] {site}: could not read post content ({str(e)[:120]}); using the stored copy")
            changed = 0
        texts = corpus(db, site)
        names = {u: title_words(u, titles.get(u)) for u in buckets}
        df = defaultdict(int)
        for chunks, _slug in names.values():
            for w in {w for c in chunks for w in c}:
                df[w] += 1
        common = {w for w, k in df.items() if k > 0.25 * max(1, len(buckets))}
        owners = defaultdict(set)                        # phrase -> pages whose title/slug has it (2 = not unique)
        for u, (chunks, slug) in names.items():
            for k in all_grams(chunks, slug):
                if len(owners[k]) < 2:
                    owners[k].add(u)
        targets = sorted((u for u, (b, f) in buckets.items() if link_gain.get(u, 0) > 0 and b not in BLOCKERS
                          and "IGNORED" not in f and (rows[u]["kind"] if u in rows else "") != "taxonomy"
                          and not UTILITY.search(([x for x in urlsplit(u).path.split("/") if x] or [""])[-1])),
                         key=lambda u: -link_gain[u])
        keys = {}                                        # phrase -> target
        for t in targets:
            chunks, slug = names[t]
            main = [w for w in chunks[0] if _topical(w)] if chunks else []
            ranked = [w for w in main if (len(w) >= 4 and not w.isdigit()) or (w.isdigit() and len(w) >= 3)] or main
            low = min((df[w] for w in ranked), default=0)
            core = {w for w in ranked if df[w] == low}
            for k, is_main in title_phrases(chunks, common).items():
                if owners[k] == {t} and _usable(k, common, is_main, set(main), core):
                    keys[k] = t
        strength = {}
        for u, (b, f) in buckets.items():
            if b in BLOCKERS or "IGNORED" in f or u not in texts or not (b == "INDEXED" or "BING_EARNER" in f):
                continue
            r = rows.get(u)
            strength[u] = (2.0 if b == "INDEXED" else 1.0) + math.log1p((r["gsc_clicks"] + r["bing_clicks"]) if r else 0)
        hits, used = defaultdict(list), defaultdict(int)     # target -> [(rank, source, start, end)]; phrase -> pages
        starts = candidate_starts(keys)
        for src, raw in texts.items():
            for key, (s, e) in candidates(raw, keys, starts).items():
                used[key] += 1
                t = keys[key]
                if src in strength and t != src and not links_to(raw, t):
                    hits[t].append((len(key) + strength[src], key, src, s, e))
        stock = {k for k, n in used.items() if n > max(3, STOCK_SHARE * len(texts))}
        per_source, made = defaultdict(int), 0
        for t in targets:
            if made >= per_site:
                break
            picked = 0
            for rank, key, src, s, e in sorted(hits.get(t, []), key=lambda h: -h[0]):
                if picked >= MAX_PER_TARGET or made >= per_site:
                    break
                if key in stock or per_source[src] >= MAX_PER_SOURCE:
                    continue
                raw = texts[src]
                hit = find(raw, raw[s:e], t)             # the occurrence apply() will link: the first safe one
                if not hit:
                    continue
                a, b, context = hit
                prev = re.search(r"([A-Za-z0-9]+)[\s-]+$", raw[max(0, a - 40):a])
                if prev and _topical(stem(prev.group(1))) and stem(prev.group(1)) in {w for c in names[t][0] for w in c}:
                    continue
                out.append({"id": link_id(src, t), "site": site, "source": src, "source_title": titles.get(src, ""),
                            "target": t, "target_title": titles.get(t, ""), "anchor": raw[a:b], "context": context,
                            "score": round(rank, 2), "gain": round(link_gain.get(t, 0), 4)})
                per_source[src] += 1
                picked += 1
                made += 1
        n_targets = len({p["target"] for p in out if p["site"] == site})
        log(f"[links] {site}: {made} link proposals with natural anchors for {n_targets} pages "
            f"({len(strength)} trusted source pages scanned, {len(stock)} stock phrases skipped, {changed} posts re-read)")
    return out


def _log_change(site, line):
    """gearuptofit: newest-first entry under today's heading in CHANGELOG-AGENTS.md (its rules require it)."""
    if site == "gearuptofit.com":
        p = config.PORTFOLIO / "01-gearuptofit" / "CHANGELOG-AGENTS.md"
        if p.exists():
            txt = p.read_text(encoding="utf-8")
            head = f"## {dt.datetime.now(dt.timezone.utc).date().isoformat()} (Index Engine link autopilot)\n\n"
            marker = "Read this before you start: it is how the next agent learns what the last one did.\n"
            if head in txt:
                txt = txt.replace(head, head + line + "\n", 1)
            elif marker in txt:
                txt = txt.replace(marker, marker + "\n" + head + line + "\n", 1)
            else:
                txt = None
            if txt is not None:
                p.write_text(txt, encoding="utf-8")
                return
    p = config.OUT / "changes.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "a", encoding="utf-8") as fh:
        fh.write(line + "\n")


CF_API = "https://api.cloudflare.com/client/v4"
EDGE_HOURS = 4          # longest edge-cache lifetime seen on these sites (gearuptofit AGENTS.md: about 4 h)


PURGE_COOLDOWN_MIN = 20


def purge(db, site, urls):
    """Clear Cloudflare's cached copy of these URLs, with the app's own token only (never other credentials).
    Returns 'purged' or why not. Needs the token to carry Zone > Cache Purge. After a refusal the same answer is
    returned for 20 minutes without asking again, so unseen links are not re-tried on every 10-minute tick."""
    held = store.get_meta(db, site).get("cf_purge_denied")
    if held and (days_since(held["at"]) or 1) * 1440 < PURGE_COOLDOWN_MIN:
        return held["why"]
    result = _purge_call(db, site, urls)
    refused = result not in ("purged", "no app token")          # a missing token file costs nothing to re-check
    store.set_meta(db, site, cf_purge_denied={"at": now_iso(), "why": result} if refused else None)
    return result


def _purge_call(db, site, urls):
    try:
        tok = config.CF_TOKEN_FILE.read_text(encoding="utf-8").split()[-1]
    except (OSError, IndexError):
        return "no app token"
    SECRETS.append(tok)
    h = {"Authorization": f"Bearer {tok}"}
    try:
        zid = store.get_meta(db, site).get("cf_zone")
        if not zid:
            z = requests.get(f"{CF_API}/zones", headers=h, params={"name": bare_host(urlsplit(config.site_base(site)).netloc)},
                             timeout=30).json().get("result") or []
            if not z:
                return "the site is not on this Cloudflare account"
            zid = z[0]["id"]
            store.set_meta(db, site, cf_zone=zid)
        r = requests.post(f"{CF_API}/zones/{zid}/purge_cache", headers=h, json={"files": urls}, timeout=30).json()
        if r.get("success"):
            return "purged"
        msg = str((r.get("errors") or [{}])[0].get("message", ""))[:80]
        return "not permitted (add Cache Purge to the app token)" if "auth" in msg.lower() or "permission" in msg.lower() else f"failed: {msg}"
    except Exception as e:
        return f"failed ({type(e).__name__})"


def visible_to_visitors(source, target):
    """Is the link on the page visitors and crawlers get (the plain URL, through any edge cache)?"""
    try:
        r = get(source, timeout=(10, 60))
        return links_to(r.text, target), r.headers.get("cf-cache-status"), r.headers.get("age")
    except Exception as e:
        return False, f"error {type(e).__name__}", None


def cached_msg(cache, age, purged=None):
    """Honest status for a link that is in WordPress but not yet in the copy Cloudflare serves."""
    try:
        cached_at = dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=int(age))
    except (TypeError, ValueError):
        cached_at = dt.datetime.now(dt.timezone.utc)
    eta = (cached_at + dt.timedelta(hours=EDGE_HOURS)).astimezone().strftime("%H:%M")      # the PC's (owner's) local time
    return (f"applied; live in WordPress, but Cloudflare still serves an older cached copy (cache {cache}). "
            f"Visitors and Google see it by about {eta}" + (f". Cache purge: {purged}" if purged else ""))


def recheck(db, report):
    """Links applied in the last 2 days but not yet seen by visitors: look again, and say so when they are."""
    rows = db.execute("""SELECT *, julianday('now') - julianday(applied_at) AS days FROM link_ops
                         WHERE status='applied' AND COALESCE(result,'') NOT LIKE '%visible to visitors%'
                         AND julianday('now') - julianday(applied_at) < 3""").fetchall()
    for op in rows:
        seen, cache, age = visible_to_visitors(op["source"], op["target"])
        if not seen and cache in ("HIT", "STALE", "UPDATING", "REVALIDATED") and purge(db, op["site"], [op["source"]]) == "purged":
            time.sleep(2)                                # e.g. Cache Purge was added to the token since the link went in
            seen, cache, age = visible_to_visitors(op["source"], op["target"])
        if seen:
            msg = f"applied; visible to visitors (checked {dt.datetime.now().strftime('%d %b %H:%M')})"
            log(f"[links] now visible to visitors: {op['source']} -> {op['target']}")
        elif op["days"] >= 2:
            msg = "applied in WordPress, but still not on the page visitors get after 2 days: open the post and check it"
        elif cache in ("HIT", "STALE", "UPDATING", "REVALIDATED"):
            msg = cached_msg(cache, age)
        else:
            continue
        if msg[:40] != (op["result"] or "")[:40]:          # report changes of state, not a re-estimated minute
            db.execute("UPDATE link_ops SET result=? WHERE id=?", (msg, op["id"]))
            report(op["id"], "applied", msg, op["applied_at"])
    db.commit()


def _site_guard_check():
    gate = config.SITE_GATES.get("gearuptofit.com")
    if not gate or not Path(gate).exists():
        return "site_guard not found"
    try:
        r = subprocess.run([sys.executable, str(gate), "check"], cwd=str(Path(gate).parent.parent), capture_output=True,
                           text=True, encoding="utf-8", errors="replace", timeout=1800)
        return {0: "CLEAN", 1: "ALERT", 2: "BLOCK"}.get(r.returncode, f"exit {r.returncode}")
    except Exception as e:
        return f"not run ({type(e).__name__})"


def apply(db, links, settings, clients, report):
    """Apply approved links and undo requested ones. report(id, status, result, applied_at) mirrors to the dashboard."""
    cap = max(0, min(MAX_PER_DAY, int(settings.get("links_per_site_per_day", 5) or 0)))
    autopilot = bool(settings.get("link_autopilot"))
    done = defaultdict(int)
    for r in db.execute("SELECT site FROM link_ops WHERE status='applied' AND julianday('now') - julianday(applied_at) < 1"):
        done[r["site"]] += 1
    touched = set()
    for lk in links:
        site, status = lk["site"], lk["status"]
        wpc = clients.get(site)
        if status == "undo":
            _undo(db, lk, wpc, report)
            continue
        auto = status == "proposed"
        if auto and (not autopilot or site in STRICT_SITES):
            continue
        if status not in ("approved", "proposed") or done[site] >= cap:
            continue
        if not wpc:
            report(lk["id"], "failed", "no WordPress login for this site", None)
            continue
        ok, msg = _apply_one(db, lk, wpc)
        report(lk["id"], "applied" if ok else "failed", msg, now_iso() if ok else None)
        if ok:
            done[site] += 1
            touched.add(site)
            _log_change(site, f"- {now_iso()[:16]}Z | Index Engine link autopilot, {'auto-approved' if auto else 'owner-approved in the dashboard'} | "
                              f"internal link on {lk['source']} -> {lk['target']} (anchor \"{lk['anchor']}\") | "
                              f"backup out/backups/links/{lk['id']}*.json; Undo in the dashboard | {msg}")
    if "gearuptofit.com" in touched:
        res = _site_guard_check()
        _log_change("gearuptofit.com", f"- {now_iso()[:16]}Z | Index Engine | site_guard.py check after the link batch: {res}")
        log(f"[links] gearuptofit site_guard check: {res}")
    db.commit()
    return touched


def _apply_one(db, lk, wpc):
    try:
        loc = locate(db, wpc, lk["source"])
        if not loc:
            return False, "source is not a published post/page"
        raw, _ = wpc.raw(*loc)
        if links_to(raw, lk["target"]):
            return False, "the source already links to the target"
        hit = find(raw, lk["anchor"], lk["target"])
        if not hit:
            return False, "the anchor text is no longer in a safe place on the source page"
        s, e, _ = hit
        new = raw[:s] + f'<a href="{lk["target"]}">' + raw[s:e] + "</a>" + raw[e:]
        _backups().mkdir(parents=True, exist_ok=True)
        bk = _backups() / f"{lk['id']}-{dt.datetime.now():%Y%m%d-%H%M%S}.json"
        bk.write_text(json.dumps({"link": lk, "post": loc, "raw_before": raw}, ensure_ascii=False), encoding="utf-8")
        wpc.update(loc[0], loc[1], new)
        after, _ = wpc.raw(*loc)
        if f'<a href="{lk["target"]}">{raw[s:e]}</a>' not in after:
            return False, "saved, but the link is not in the stored content (check the post)"
        fresh = False
        for _ in range(2):                       # the server's own copy (cache bypassed)
            try:
                fresh = links_to(get(f"{lk['source']}?nc={int(time.time())}", timeout=(10, 60)).text, lk["target"])
                if fresh:
                    break
            except Exception:
                pass
            time.sleep(3)
        purged = purge(db, lk["site"], [lk["source"]])
        time.sleep(2)
        seen, cache, age = visible_to_visitors(lk["source"], lk["target"])
        if seen:
            msg = "applied; visible to visitors"
        elif fresh:
            msg = cached_msg(cache, age, purged)
        else:
            msg = "applied in WordPress; the page has not picked it up yet (page cache). Re-checked every tick"
        db.execute("INSERT OR REPLACE INTO link_ops VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                   (lk["id"], lk["site"], lk["source"], lk["target"], raw[s:e], loc[0], loc[1], "applied", now_iso(),
                    str(bk), msg))
        return True, msg
    except Exception as e:
        return False, f"{type(e).__name__}: {str(e)[:160]}"


def _undo(db, lk, wpc, report):
    op = db.execute("SELECT * FROM link_ops WHERE id=?", (lk["id"],)).fetchone()
    if not op or not wpc:
        report(lk["id"], "failed", "nothing recorded to undo on this PC", None)
        return
    try:
        raw, _ = wpc.raw(op["post_id"], op["post_type"])
        added = f'<a href="{op["target"]}">{op["anchor"]}</a>'
        if added not in raw:
            report(lk["id"], "undone", "the link was already gone", None)
        else:
            wpc.update(op["post_id"], op["post_type"], raw.replace(added, op["anchor"], 1))
            purged = purge(db, lk["site"], [op["source"]])
            report(lk["id"], "undone", f"link removed (cache purge: {purged})", None)
        db.execute("UPDATE link_ops SET status='undone' WHERE id=?", (lk["id"],))
        _log_change(lk["site"], f"- {now_iso()[:16]}Z | Index Engine link autopilot | UNDO: removed the link {op['source']} -> {op['target']}")
    except Exception as e:
        report(lk["id"], "failed", f"undo failed: {type(e).__name__}: {str(e)[:120]}", None)
