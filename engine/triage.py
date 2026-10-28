# -*- coding: utf-8 -*-
"""Stage 5 - decide, per URL, why it is not indexed and the one thing that would change that.

Combines the live audit, Google's own verdict (URL Inspection or Search impressions), Bing
traffic and crawl status, the internal link graph and near-duplicate detection. Also produces:
  * a site-level diagnosis (is this a discovery problem or a site-wide quality judgment?),
  * an internal-link plan: which already-indexed, related pages should link to each stuck page,
  * a short daily queue for the manual "Request indexing" button, limited to URLs where a
    request can actually help.
Read-only. It recommends; it never edits a site.
"""
import json
import math
import re
from collections import Counter, defaultdict

from . import config, store
from .google import state_of
from .util import days_since, now_iso, parse_ts

STOP = set("""a an the and or but of to in on for with at by from as is are be was were it its this that these those your you
our we i my me how what why when where which who whom vs versus best top guide complete ultimate tips ways things
review reviews new 2023 2024 2025 2026 2027 can do does should will get make using use into about more most than
need know every all any one two three five ten simple easy quick really actually here there their them they""".split())
STALE_STATES = ("noindex", "redirect", "not found", "404", "soft 404", "blocked", "server error", "5xx", "redirect error")
MANUAL_ORDER = ["GOOGLE_STALE", "UNKNOWN_TO_GOOGLE", "DISCOVERED_NOT_INDEXED", "CRAWLED_NOT_INDEXED"]
SOFT404 = re.compile(r"\b(page not found|not found|404|nothing found|no results)\b", re.I)
BLOCKERS = {"FETCH_FAILED", "BROKEN", "SERVER_ERROR", "REDIRECTS", "NOINDEX", "ROBOTS_BLOCKED", "CANONICAL_ELSEWHERE", "NOT_HTML"}
LABELS = {"NOINDEX": "noindexed", "REDIRECTS": "redirected", "BROKEN": "broken", "SERVER_ERROR": "server error",
          "ROBOTS_BLOCKED": "robots-blocked", "CANONICAL_ELSEWHERE": "canonical elsewhere", "FETCH_FAILED": "unreachable",
          "NOT_HTML": "not HTML"}


def plural(n, word):
    return f"{n:,} {word}{'' if n == 1 else 's'}"


def _g(r, k, default=0):
    try:
        v = r[k]
    except (IndexError, KeyError):
        return default
    return default if v is None else v


def value_score(r):
    """How much a URL is worth to the owner. Clicks on either engine dominate; recency and depth break ties."""
    v = 5 * (_g(r, "bing_clicks") + _g(r, "gsc_clicks")) + 0.05 * (_g(r, "bing_impr") + _g(r, "gsc_impr"))
    if _g(r, "kind", "") in ("post", "page", "product"):
        v += 1
    age = days_since(_g(r, "lastmod", None))
    if age is not None and age < 60:
        v += 1.5
    v += min(_g(r, "words"), 2000) / 1000
    return round(v, 2)


def tokens(text):
    out = set()
    for w in re.findall(r"[a-z0-9]+", (text or "").lower()):
        if len(w) < 3 or w in STOP or w.isdigit():
            continue
        out.add(w[:-1] if len(w) > 4 and w.endswith("s") and not w.endswith("ss") else w)
    return out


def clean_title(t):
    return re.split(r"\s+[|–—-]\s+(?=[^|–—-]*$)", t or "")[0].strip().lower()


def near_duplicates(pages, threshold=0.5):
    """pages: {url: sketch list}. Returns {url: (other_url, est_jaccard)} using bottom-k sketches."""
    pages = {u: sk for u, sk in pages.items() if len(sk) >= 30}
    if len(pages) < 2:
        return {}
    df = Counter(h for sk in pages.values() for h in sk)
    cap = max(3, int(0.02 * len(pages)))
    inv = defaultdict(list)
    for u, sk in pages.items():
        for h in sk:
            if df[h] <= cap:
                inv[h].append(u)
    pair = Counter()
    for us in inv.values():
        for i in range(len(us)):
            for j in range(i + 1, len(us)):
                pair[(us[i], us[j]) if us[i] < us[j] else (us[j], us[i])] += 1
    best = {}
    for (a, b), n in pair.items():
        if n < 5:
            continue
        sa, sb = set(pages[a]), set(pages[b])
        k = min(len(sa), len(sb))
        union_bottom = sorted(sa | sb)[:k]
        j = sum(1 for h in union_bottom if h in sa and h in sb) / k
        if j >= threshold:
            for x, y in ((a, b), (b, a)):
                if j > best.get(x, ("", 0))[1]:
                    best[x] = (y, round(j, 2))
    return best


def load_site(db, site):
    return db.execute("""
        SELECT u.url, u.kind, u.lastmod, u.in_sitemap, u.in_wp,
               a.checked_at, a.status, a.final_url, a.hops, a.ctype, a.ms, a.noindex, a.noindex_src, a.robots_google,
               a.robots_bing, a.canonical, a.canonical_state, a.title, a.h1, a.words, a.sketch, a.error AS aerr, a.trust,
               a.bytes, a.inline_b64,
               u.published,
               g.inspected_at, g.verdict, g.coverage, g.last_crawl, g.google_canonical, g.error AS gerr,
               COALESCE(p.gsc_clicks,0) AS gsc_clicks, COALESCE(p.gsc_impr,0) AS gsc_impr,
               COALESCE(p.gsc_recent_impr,0) AS recent, COALESCE(p.bing_clicks,0) AS bing_clicks,
               COALESCE(p.bing_impr,0) AS bing_impr,
               b.checked_at AS bing_checked, b.last_crawled AS bing_crawled,
               (SELECT MAX(submitted_at) FROM submissions s WHERE s.url=u.url AND s.http_status BETWEEN 200 AND 299) AS submitted_at
        FROM urls u LEFT JOIN audits a ON a.url=u.url LEFT JOIN gsc g ON g.url=u.url
        LEFT JOIN perf p ON p.url=u.url LEFT JOIN bing_info b ON b.url=u.url
        WHERE u.site=? AND (u.in_sitemap=1 OR u.in_wp=1)""", (site,)).fetchall()


def link_graph(db, site, urls):
    """Inlinks counted from this site's audited pages.
    Returns ({url_without_slash: (all_inlinks, body_inlinks)}, {src: {dst_without_slash}})."""
    keys = {u.rstrip("/") for u in urls}
    counts = defaultdict(lambda: [set(), set()])
    out_of = defaultdict(set)
    for r in db.execute("SELECT l.src, l.dst, l.in_content FROM links l JOIN urls u ON u.url=l.src "
                        "JOIN audits a ON a.url=l.src AND a.status=200 AND a.hops=0 WHERE u.site=?", (site,)):
        d = r["dst"].rstrip("/")
        out_of[r["src"]].add(d)
        if d in keys and r["src"].rstrip("/") != d:
            counts[d][0].add(r["src"])
            if r["in_content"]:
                counts[d][1].add(r["src"])
    return {k: (len(a), len(c)) for k, (a, c) in counts.items()}, out_of


def classify(r, inl, audit_coverage, dup, dup_title, site=""):
    flags = []
    words = _g(r, "words")
    if r["bing_clicks"]:
        flags.append("BING_EARNER")
    if r["status"] == 200 and words < config.THIN_WORDS and r["kind"] != "taxonomy":
        flags.append("THIN")
    if (r["ms"] or 0) > config.SLOW_MS:
        flags.append("SLOW")
    if r["status"] == 200 and SOFT404.search(r["title"] or "") and words < 400:
        flags.append("SOFT_404")
    all_in, body_in = inl.get(r["url"].rstrip("/"), (0, 0))
    if audit_coverage >= 0.9 and r["status"] == 200:
        if all_in == 0:
            flags.append("ORPHAN")
        elif body_in == 0:
            flags.append("NO_BODY_LINKS")
    if dup:
        flags.append("NEAR_DUPLICATE")
    if dup_title:
        flags.append("DUPLICATE_TITLE")
    if r["submitted_at"] and not r["bing_crawled"] and r["bing_checked"]:
        flags.append("BING_NOT_CRAWLED")

    # ---- technical blockers (fix these; no search engine will index the URL as it is)
    if r["checked_at"] is None:
        return "NOT_CHECKED", flags, "Not fetched yet.", "Run the audit stage."
    if r["status"] is None:
        return ("FETCH_FAILED", flags, f"Fetch failed: {(r['aerr'] or 'no response')[:120]}",
                "The server did not answer a normal crawler request. Fix hosting/caching; crawlers slow down on sites that time out.")
    if r["aerr"] == "bot challenge":
        return ("FETCH_FAILED", flags, f"HTTP {r['status']} bot challenge",
                "A firewall (e.g. Cloudflare) challenged the audit's fetcher. Make sure verified Googlebot and bingbot are allowed; "
                "confirm with URL Inspection's live test.")
    if r["status"] >= 500:
        return "SERVER_ERROR", flags, f"HTTP {r['status']}", "Server error. Fix it before anything else; repeated 5xx makes crawlers back off site-wide."
    if r["status"] >= 400:
        return ("BROKEN", flags, f"HTTP {r['status']}",
                "Listed for indexing but broken. Restore the page or drop it from the sitemap. Check 16 months of Bing and Google clicks before redirecting it.")
    if r["hops"]:
        if r["in_sitemap"]:
            return ("REDIRECTS", flags, f"Redirects to {r['final_url']}",
                    "The sitemap lists a URL that redirects. List the final URL instead (engines index the target, not this one).")
        if site in config.DELIBERATE_REDIRECT_SITES:
            return ("REDIRECTS", flags, f"Published post redirects to {r['final_url']}",
                    "Deliberate legacy redirect (the site's AGENTS.md). Leave it; do not republish or restore it.")
        return ("REDIRECTS", flags + ["PUBLISHED_BUT_REDIRECTED"], f"Published post redirects to {r['final_url']}",
                "Still published in WordPress but redirected, so it can never be indexed (the target is). If the merge was "
                "intended, move the post to draft/trash and keep the redirect; if not, remove the redirect. "
                "Check its Bing clicks before deciding (guardrail 1).")
    if "html" not in (r["ctype"] or ""):
        return "NOT_HTML", flags, f"Content-Type {r['ctype']}", "Not an HTML page; remove it from the sitemap unless that is intended."
    if r["noindex"]:
        return ("NOINDEX", flags, f"noindex via {r['noindex_src']}",
                "Tells engines not to index it. If it should rank, remove the noindex; if not, remove it from the sitemap.")
    if not r["robots_google"] or not r["robots_bing"]:
        who = " and ".join(n for n, ok in (("Googlebot", r["robots_google"]), ("bingbot", r["robots_bing"])) if not ok)
        return "ROBOTS_BLOCKED", flags, f"robots.txt blocks {who}", "Allow it in robots.txt or remove it from the sitemap."
    if r["canonical_state"] == "other":
        return ("CANONICAL_ELSEWHERE", flags, f"canonical -> {r['canonical']}",
                "The page names another URL as canonical, so engines index that one. Make it self-canonical if this page should rank; otherwise drop it from the sitemap.")

    # ---- the page is indexable: what does Google say?
    if r["in_wp"] and not r["in_sitemap"] and r["url"] not in config.INTENTIONAL_OUT_OF_SITEMAP:
        flags.append("NOT_IN_SITEMAP")              # only here: sitemap plugins rightly omit noindexed/redirected posts
    st = state_of(r["coverage"], r["verdict"])
    cov = r["coverage"] or ""
    if r["recent"] or st == "indexed":
        return "INDEXED", flags, cov or f"{r['recent']} Google impressions in the last {config.EVIDENCE_DAYS} days", ""
    if st == "other" and any(s in cov.lower() for s in STALE_STATES):
        return ("GOOGLE_STALE", flags, f"Google last saw: {cov} (crawled {(r['last_crawl'] or '?')[:10]})",
                "That problem is fixed on the live page. Request indexing in Search Console so Google re-reads it.")
    if st == "other" and ("duplicate" in cov.lower() or "alternate" in cov.lower()):
        return ("GOOGLE_PICKED_OTHER", flags, f"{cov} -> {r['google_canonical'] or '?'}",
                "Google treats this as a copy of another URL. Make it clearly different, or merge the two (owner approval; check both pages' Bing clicks first).")
    if st == "unknown":
        return ("UNKNOWN_TO_GOOGLE", flags, cov,
                "Google has never seen this URL. Make sure a sitemap Google reads lists it and that indexed pages link to it, then request indexing.")
    if st == "discovered":
        return ("DISCOVERED_NOT_INDEXED", flags, cov,
                "Google knows the URL but has not spent a crawl on it: it rates the URL (or the site) as low priority. "
                "Links from pages Google already indexes are the strongest lever; resubmitting is not.")
    if st == "crawled":
        changed = parse_ts(r["lastmod"]) and parse_ts(r["last_crawl"]) and parse_ts(r["lastmod"]) > parse_ts(r["last_crawl"])
        why = f"{cov} (crawled {(r['last_crawl'] or '?')[:10]}{', changed since' if changed else ''})"
        return ("CRAWLED_NOT_INDEXED", flags, why,
                "Google read it and decided it is not worth indexing. Resubmitting changes nothing until the page itself changes: "
                "add something only this page offers, merge it into a stronger page, or drop it.")
    if r["gerr"]:
        return "NOT_INSPECTED", flags, f"Inspection error: {r['gerr'][:100]}", "Retried on a later run."
    return "NOT_INSPECTED", flags, "No Google data yet", "Will be inspected on a later run (URL Inspection quota is 2,000/day per property)."


def detail(bucket, flags, r, dup, dup_title, inl):
    """Concrete extra instructions from the flags."""
    out = []
    if bucket in BLOCKERS:                            # link/content advice is moot until the block is resolved
        if "BING_EARNER" in flags:
            out.append(f"It earned {plural(r['bing_clicks'], 'Bing click')}: this block removes it from Bing too. Reconsider.")
        return " ".join(out)
    if "BING_EARNER" in flags:
        out.append(f"KEEP LIVE: earns Bing clicks ({r['bing_clicks']}). Never noindex, redirect or merge it away.")
    if "NOT_IN_SITEMAP" in flags:
        out.append("Published but missing from every sitemap: fix the sitemap plugin settings for this post type.")
    if "ORPHAN" in flags:
        out.append("No page on the site links to it: add 2-3 links from related indexed pages (internal_link_plan.csv).")
    elif "NO_BODY_LINKS" in flags:
        out.append("Linked only from menus/sidebars, never from article text: add contextual links (internal_link_plan.csv).")
    if "THIN" in flags:
        out.append(f"Only {r['words']} words of main content.")
    if "NEAR_DUPLICATE" in flags and dup:
        out.append(f"Main text ~{int(dup[1] * 100)}% the same as {dup[0]}: differentiate or merge.")
    if "DUPLICATE_TITLE" in flags:
        out.append(f"Same title as {dup_title}.")
    if "SLOW" in flags:
        out.append(f"Took {r['ms'] / 1000:.0f} s to load.")
    if "TRUST_RISK" in flags:
        hits = json.loads(r["trust"] or "[]")
        out.append(f"{len(hits)} phrasing(s) read as first-hand experience or statistics, e.g. \"{hits[0]['excerpt'][:140]}\": "
                   "give each a named, checkable source or remove it (portfolio guardrail 4).")
    if "HEAVY_PAGE" in flags:
        mb = (r["bytes"] or 0) / 1_000_000
        img = f", of which {(r['inline_b64'] or 0) / 1000:,.0f} KB is image data pasted into the HTML" if r["inline_b64"] else ""
        out.append(f"The server sends {mb:.1f} MB of HTML{img}. Move pictures to the media library and remove pasted base64 images "
                   "(a data: URI is also not a valid image URL in structured data); a normal article here is about 0.2 MB.")
    if "SOFT_404" in flags:
        out.append("Looks like an error page (title says not found).")
    return " ".join(out)


def bing_state(r):
    if r["bing_clicks"]:
        return plural(r["bing_clicks"], "click")
    if r["bing_crawled"]:
        return f"crawled {r['bing_crawled']}"
    if r["submitted_at"]:
        return f"submitted {r['submitted_at'][:10]}" + (" - not crawled yet" if r["bing_checked"] else "")
    return ""


def link_plan(rows, buckets, inl_src, per_site=200):
    """For stuck pages, suggest related pages that are already indexed (or earn Bing clicks) as link sources."""
    by_url = {r["url"]: r for r in rows}
    toks = {u: tokens(clean_title(r["title"]) + " " + (r["h1"] or "")) for u, r in by_url.items()}
    df = Counter(t for ts in toks.values() for t in ts)
    common = {t for t, n in df.items() if n > max(5, 0.25 * len(toks))}     # brand/topic words on every page
    toks = {u: ts - common for u, ts in toks.items()}
    idf = {t: math.log((1 + len(toks)) / (1 + n)) + 1 for t, n in df.items()}  # rare shared words count for more

    def wsim(a, b):
        return sum(idf[t] for t in a & b) / max(1e-9, sum(idf[t] for t in a | b))
    # A source must serve its own content (not noindexed, redirected...) and be trusted: indexed, or earning Bing clicks.
    sources = [u for u, (b, f) in buckets.items()
               if b not in BLOCKERS and "IGNORED" not in f and (b == "INDEXED" or "BING_EARNER" in f)]
    inv = defaultdict(list)
    for u in sources:
        for t in toks[u]:
            inv[t].append(u)
    stuck = [u for u, (b, f) in buckets.items()
             if b in ("UNKNOWN_TO_GOOGLE", "DISCOVERED_NOT_INDEXED", "CRAWLED_NOT_INDEXED", "NOT_INSPECTED", "GOOGLE_STALE")
             and ("ORPHAN" in f or "NO_BODY_LINKS" in f or b != "NOT_INSPECTED") and "THIN" not in f and "IGNORED" not in f]
    stuck.sort(key=lambda u: -value_score(by_url[u]))
    plan = []
    for u in stuck[:per_site]:
        cand = Counter()
        for t in toks[u]:
            for s in inv[t]:
                if s != u:
                    cand[s] += 1
        scored = []
        for s, _n in cand.items():
            if u.rstrip("/") in inl_src.get(s, set()):
                continue                                       # already links to it
            if len(toks[u] & toks[s]) < 2:
                continue
            j = wsim(toks[u], toks[s])
            if j >= 0.25:
                src = by_url[s]
                scored.append((j * (1 + math.log1p(src["bing_clicks"] + src["gsc_clicks"])), j, s))
        for _score, j, s in sorted(scored, reverse=True)[:3]:
            plan.append({"target": u, "target_title": by_url[u]["title"], "bucket": buckets[u][0], "source": s,
                         "source_title": by_url[s]["title"], "shared_terms": " ".join(sorted(toks[u] & toks[s])),
                         "similarity": round(j, 2), "source_bing_clicks": by_url[s]["bing_clicks"],
                         "source_google_clicks": by_url[s]["gsc_clicks"]})
    return plan


def site_diagnosis(counts, meta, missing=0, conflicts=0, excluded=None, blocked_earners=(0, 0), near_dup=(0, 0), thin=0,
                   trust=0, heavy=0):
    """Plain-English verdict for the whole site."""
    excluded = excluded or Counter()
    indexable = sum(n for b, n in counts.items() if b not in BLOCKERS and b != "NOT_CHECKED")
    known = sum(counts.get(b, 0) for b in ("INDEXED", "CRAWLED_NOT_INDEXED", "DISCOVERED_NOT_INDEXED", "UNKNOWN_TO_GOOGLE",
                                           "GOOGLE_STALE", "GOOGLE_PICKED_OTHER"))
    msgs = []
    if conflicts:
        msgs.append(("fix", f"{plural(conflicts, 'URL')} listed in a sitemap but blocked (noindex, redirect, error, robots or canonical "
                            "elsewhere). Mixed signals: fix each page or drop it from the sitemap (fix_technical.csv)."))
    if missing:
        msgs.append(("fix", f"Indexable published posts/pages missing from every sitemap: {missing} (fix_technical.csv)."))
    if near_dup[0] >= 10:
        msgs.append(("site", f"{near_dup[0]} of {indexable} indexable pages share 50%+ of their main text with another page on the "
                             f"site (near_duplicates.csv); Bing clicks reach {near_dup[1]} of them. Templated near-copies are what "
                             "Google most often leaves 'crawled, not indexed', and at scale they weigh on how it rates the whole "
                             "site. Consolidate the ones with no clicks on either engine into fewer, distinct pages "
                             "(owner decision, one batch at a time)."))
    if trust >= 5:
        msgs.append(("site", f"{trust} indexable pages contain phrasings that read as invented first-hand experience, ratings or "
                             "statistics (flag TRUST_RISK; the sentences are on each URL). Google's quality systems weigh this "
                             "site-wide: source each claim or remove it, worst pages first."))
    if heavy:
        msgs.append(("fix", f"{plural(heavy, 'indexable page')} {'is' if heavy == 1 else 'are'} very heavy: over 1 MB of HTML, or carrying "
                            "whole images pasted in as base64, in the page or its schema (flag HEAVY_PAGE; the size is on each URL). "
                            "Heavy pages are slower for visitors and crawlers: remove the pasted image data."))
    if thin >= 10:
        msgs.append(("info", f"{thin} indexable pages have under {config.THIN_WORDS} words of main content in the HTML the server "
                             "sends (hub/listing pages can be fine; articles rarely are)."))
    if sum(excluded.values()):
        parts = ", ".join(f"{n} {LABELS.get(b, b).lower()}" for b, n in excluded.most_common())
        msgs.append(("info", f"Published posts kept out of the index and out of the sitemaps: {sum(excluded.values())} ({parts}). "
                             "Expected if that was the plan (e.g. a prune); they will never be indexed."))
    if blocked_earners[0]:
        msgs.append(("fix", f"Blocked or excluded pages that had earned Bing clicks: {blocked_earners[0]} "
                            f"({plural(blocked_earners[1], 'click')} in the Bing data). noindex and redirects remove them from Bing "
                            "too: review those first (fix_technical.csv)."))
    if known >= 20:
        idx = counts.get("INDEXED", 0) / known
        quality = (counts.get("CRAWLED_NOT_INDEXED", 0) + counts.get("DISCOVERED_NOT_INDEXED", 0)) / known
        unknown = counts.get("UNKNOWN_TO_GOOGLE", 0) / known
        reads = [sm for sm in meta.get("gsc_sitemaps") or [] if sm.get("lastDownloaded")]
        fresh = min(reads, key=lambda sm: days_since(sm["lastDownloaded"]) or 0) if reads else None
        fresh = fresh if fresh and (days_since(fresh["lastDownloaded"]) or 99) <= 30 else None
        if idx < 0.5 and (quality >= 0.3 or (unknown >= 0.3 and fresh)):
            seen = (f" and has not even queued {unknown:.0%}, although it read {fresh['path'].rsplit('/', 1)[-1]} on "
                    f"{fresh['lastDownloaded'][:10]}" if unknown >= 0.2 and fresh else "")
            msgs.append(("site", f"Google indexes {idx:.0%} of the checked pages. It looked at, then passed over, {quality:.0%}{seen}. "
                                  "That pattern is a judgment about the whole site, not a discovery problem: re-submitting URLs, "
                                  "pinging, or any 'instant indexing' service will not change it. What does: fewer, better pages "
                                  "(merge or drop pages with no clicks on either engine), real, checkable substance on the pages "
                                  "that remain, and links from the pages Google already trusts."))
        elif unknown >= 0.2:
            msgs.append(("discovery", f"{unknown:.0%} of checked pages are unknown to Google and Search Console has not read a "
                                      "sitemap in the last 30 days. Resubmit the sitemap and link these pages from indexed ones."))
        if idx >= 0.8:
            msgs.append(("ok", f"Google indexes {idx:.0%} of checked pages. Keep new pages linked from day one."))
    for sm in meta.get("gsc_sitemaps") or []:
        if sm.get("errors"):
            msgs.append(("fix", f"Search Console reports {sm['errors']} errors in sitemap {sm['path']}."))
        if sm.get("lastDownloaded") and (days_since(sm["lastDownloaded"]) or 0) > 30:
            msgs.append(("discovery", f"Google last read {sm['path']} {days_since(sm['lastDownloaded']):.0f} days ago."))
    if not indexable and not known:
        msgs.append(("info", "No audited URLs yet."))
    return msgs


def run(db, sites, with_manual=True):
    """Classify every URL of each site. with_manual=False leaves the request list to brain.analyze (ranked by odds)."""
    results = {}
    now = now_iso()
    for site in sites:
        rows = load_site(db, site)
        if not rows:
            continue
        audited = sum(1 for r in rows if r["checked_at"])
        coverage = audited / len(rows)
        inl, inl_src = link_graph(db, site, [r["url"] for r in rows])
        # Redirected and non-canonical URLs show another page's content; leave them out of duplicate checks.
        own = [r for r in rows if r["status"] == 200 and not r["noindex"] and not r["hops"] and r["canonical_state"] != "other"]
        dups = near_duplicates({r["url"]: json.loads(r["sketch"] or "[]") for r in own})
        title_groups = defaultdict(list)
        for r in own:
            if r["title"]:
                title_groups[clean_title(r["title"])].append(r["url"])
        dup_title = {u: next(x for x in us if x != u) for us in title_groups.values() if len(us) > 1 for u in us}

        owner = {o["url"]: o for o in db.execute("SELECT url, ignored, requested_at FROM owner_state")}
        buckets, counts = {}, Counter()
        db.execute("DELETE FROM triage WHERE site=?", (site,))
        for r in rows:
            bucket, flags, reason, action = classify(r, inl, coverage, dups.get(r["url"]), dup_title.get(r["url"]), site)
            if owner.get(r["url"]) and owner[r["url"]]["ignored"]:
                flags.append("IGNORED")
            if r["trust"] and bucket not in BLOCKERS:
                flags.append("TRUST_RISK")
            if bucket not in BLOCKERS and ((r["bytes"] or 0) >= config.HEAVY_PAGE_BYTES or (r["inline_b64"] or 0) >= config.HEAVY_B64_CHARS):
                flags.append("HEAVY_PAGE")
            extra = detail(bucket, flags, r, dups.get(r["url"]), dup_title.get(r["url"]), inl)
            if r["inspected_at"] and r["coverage"] and bucket not in BLOCKERS and not r["recent"]:
                reason += f" [Google checked {r['inspected_at'][:10]}]"
            if bucket == "INDEXED":
                action = ""
            elif extra:
                action = (action + " " + extra).strip()
            if "IGNORED" in flags:
                action = "Ignored by the owner (dashboard): never submitted, kept off the work lists. " + action
            buckets[r["url"]] = (bucket, flags)
            counts[bucket] += 1
            db.execute("INSERT INTO triage(url, site, bucket, flags, reason, action, value, google, bing) VALUES(?,?,?,?,?,?,?,?,?)",
                       (r["url"], site, bucket, ",".join(flags), reason, action, value_score(r),
                        r["coverage"] or ("impressions" if r["recent"] else ""), bing_state(r)))
        meta = store.get_meta(db, site)
        blocked = [r for r in rows if buckets[r["url"]][0] in BLOCKERS]
        conflicts = sum(1 for r in blocked if r["in_sitemap"])
        excluded = Counter(buckets[r["url"]][0] for r in blocked if not r["in_sitemap"])
        earners = [r for r in blocked if r["bing_clicks"]]
        nd = [r for r in rows if "NEAR_DUPLICATE" in buckets[r["url"]][1] and buckets[r["url"]][0] not in BLOCKERS]
        thin = sum(1 for b, f in buckets.values() if "THIN" in f and b not in BLOCKERS)
        trust = sum(1 for b, f in buckets.values() if "TRUST_RISK" in f)
        heavy = sum(1 for b, f in buckets.values() if "HEAVY_PAGE" in f)
        diag = site_diagnosis(counts, meta, sum(1 for _, f in buckets.values() if "NOT_IN_SITEMAP" in f), conflicts, excluded,
                              (len(earners), sum(r["bing_clicks"] for r in earners)),
                              (len(nd), sum(1 for r in nd if r["bing_clicks"])), thin, trust, heavy)
        plan = link_plan(rows, buckets, inl_src)
        manual = manual_queue(db, site, rows, buckets, owner) if with_manual else []
        snap = {"total": len(rows), "audited": audited, "buckets": dict(counts), "conflicts": conflicts,
                "excluded": sum(excluded.values()), "bing_earners": sum(1 for b, f in buckets.values() if "BING_EARNER" in f)}
        db.execute("INSERT INTO snapshots VALUES(?,?,?)", (now, site, json.dumps(snap)))
        db.commit()
        results[site] = {"counts": counts, "diagnosis": diag, "plan": plan, "manual": manual, "dups": dups,
                         "snapshot": snap, "meta": meta, "rows": rows, "buckets": buckets, "owner": owner}
    return results


def manual_queue(db, site, rows, buckets, owner=None, gain=None):
    """Today's short list for Search Console's 'Request indexing' button, where a request can help.
    gain: {url: expected gain from a request} from the odds model; it orders pages within each tier."""
    owner = owner or {}
    gain = gain or {}
    by_url = {r["url"]: r for r in rows}
    eligible = []
    for u, (b, f) in buckets.items():
        r = by_url[u]
        if "THIN" in f or "SOFT_404" in f or "NEAR_DUPLICATE" in f or "IGNORED" in f:
            continue                                  # a request cannot help until the page itself is fixed
        done = owner.get(u) and owner[u]["requested_at"]
        if done and (days_since(done) or 0) < config.MANUAL_REQUEUE_DAYS:
            continue                                  # the owner requested it in Search Console recently
        ok = b in ("UNKNOWN_TO_GOOGLE", "GOOGLE_STALE", "DISCOVERED_NOT_INDEXED")
        if b == "CRAWLED_NOT_INDEXED":
            lm, lc = parse_ts(r["lastmod"]), parse_ts(r["last_crawl"])
            ok = bool(lm and lc and lm > lc)          # only if the page changed after Google's last look
        if not ok:
            continue
        q = db.execute("SELECT queued_at FROM manual_queue WHERE url=?", (u,)).fetchone()
        age = days_since(q["queued_at"]) if q else None
        # Best odds first: a fixed problem, then never seen, then seen-not-crawled; a crawled page whose lastmod
        # moved goes last, because lastmod also moves on re-saves without real edits.
        odds = MANUAL_ORDER.index(b)
        worth = -(gain[u] if u in gain else value_score(r))
        if age is not None and age < 0.8:
            eligible.append((0, odds, worth, u))     # already on today's list: keep it there on re-runs
        elif age is None or age >= config.MANUAL_REQUEUE_DAYS:
            eligible.append((1, odds, worth, u))
    eligible.sort()
    picked = [u for _, _, _, u in eligible[:config.MANUAL_QUEUE_PER_SITE]]
    for u in picked:
        db.execute("INSERT INTO manual_queue VALUES(?,?) ON CONFLICT(url) DO UPDATE SET queued_at="
                   "CASE WHEN julianday('now') - julianday(manual_queue.queued_at) < 0.8 THEN manual_queue.queued_at "
                   "ELSE excluded.queued_at END", (u, now_iso()))
    return [{"url": u, "bucket": buckets[u][0], "value": value_score(by_url[u])} for u in picked]
