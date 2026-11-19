# -*- coding: utf-8 -*-
"""Recovery: is each engine still showing this site at all, and if it has stopped, what to do with every page.

Two questions the rest of the engine did not answer:

1. Visibility. The traffic verdicts compare the latest 28 days with the 28 before. A site that collapsed months ago
   reads as "stable" on that test (stable at almost zero), and a site Google has dropped keeps getting
   submissions, request lists and link work that cannot help it. This compares the latest 14 days of impressions
   with the site's own reference level over the past ~13 months (the 80th percentile of its 28-day averages, last six
   weeks left out), and finds the date of the biggest step down. A collapse makes the site's first priority "rule out a
   manual action, then cut and fix", not "submit more".

2. Prune plan. When Google judges a whole site (most pages crawled or discovered but not indexed, or visibility
   collapsed), the lever is fewer, better pages. Every indexable post and page gets one decision with its evidence:
     keep      earns clicks or real impressions on either engine, or is the home page / a hub
     fix       has search demand but carries a trust or thin-content flag: keep the URL, fix the page
     merge     a near-duplicate of a stronger page on the site: move anything unique, then 301 to it
     redirect  no search value, but other sites link to it (Bing's inbound-link report): 301 to the closest page
     noindex   no clicks and next to no impressions on either engine, published long enough to have earned them, and
               thin, flagged, or left out of Google's index: noindex it (reversible) and drop it from the sitemap
     review    none of the rules above decide it: the owner's call
   Nothing here edits a site. The plan is evidence for the owner, applied one batch at a time.
"""
import datetime as dt
import html
import re
import zlib
from collections import Counter, defaultdict
from urllib.parse import quote, urlsplit

from .triage import BLOCKERS, clean_title, tokens
from .util import days_since

RECENT_DAYS = 14
GUARD_DAYS = 42                 # the reference level leaves out the latest six weeks
REF_DAYS = 400
REF_PCTL = 0.8
MIN_REF_DAILY = 10              # impressions/day the reference must reach before a drop can be judged
COLLAPSED, SEVERE, DECLINING = 0.2, 0.5, 0.8
STEP_WINDOW = 14

KEEP_IMPR = 100                 # impressions on either engine that count as real search demand
DEMAND_IMPR = 20                # ... and that count as some demand (fix instead of prune)
MIN_AGE_DAYS = 120              # a page younger than this has not had time to earn impressions: never pruned
HUB_KINDS = {"category", "tag", "taxonomy", "author", "archive"}
NOT_INDEXED = {"CRAWLED_NOT_INDEXED", "DISCOVERED_NOT_INDEXED", "UNKNOWN_TO_GOOGLE", "GOOGLE_PICKED_OTHER"}
DECISIONS = ("keep", "fix", "merge", "redirect", "noindex", "review")
ENGINE = {"google": "Google", "bing": "Bing"}
VIS_STATES = ("collapsed", "severe_drop", "declining", "stable", "growing", "never_visible", "too_little_data")


# ---------------------------------------------------------------- visibility

def _daily(db, site, src):
    rows = db.execute("SELECT day, impr, clicks FROM traffic_daily WHERE site=? AND src=? ORDER BY day", (site, src)).fetchall()
    if not rows:
        return []
    first, last = dt.date.fromisoformat(rows[0][0]), dt.date.fromisoformat(rows[-1][0])
    have = {r[0]: (r[1] or 0, r[2] or 0) for r in rows}
    out, d = [], first
    while d <= last:                   # days an engine did not report are days with no impressions
        out.append((d.isoformat(), *have.get(d.isoformat(), (0, 0))))
        d += dt.timedelta(days=1)
    return out


def _pctl(xs, p):
    xs = sorted(xs)
    if not xs:
        return 0.0
    k = (len(xs) - 1) * p
    lo = int(k)
    hi = min(lo + 1, len(xs) - 1)
    return xs[lo] + (xs[hi] - xs[lo]) * (k - lo)


def _mean(xs):
    return sum(xs) / len(xs) if xs else 0.0


def step_down(series, window=STEP_WINDOW, lookback=240):
    """The day the biggest sustained fall began: the split point where the mean of the `window` days before it most
    exceeds the mean of the `window` days after it (relative). None when nothing fell."""
    impr = [x[1] for x in series]
    best = None
    start = max(window, len(impr) - lookback)
    for i in range(start, len(impr) - window + 1):
        before, after = _mean(impr[i - window:i]), _mean(impr[i:i + window])
        if before <= 0:
            continue
        drop = (before - after) / before
        if best is None or drop > best[0]:
            best = (drop, i, before, after)
    if not best or best[0] < 0.4:
        return None
    drop, i, before, after = best
    return {"day": series[i][0], "before": round(before, 1), "after": round(after, 1), "drop": round(drop, 3)}


def engine_visibility(series, min_ref=MIN_REF_DAILY):
    """Visibility verdict for one engine's daily (day, impressions, clicks) series."""
    if len(series) < GUARD_DAYS + 28:
        return {"state": "too_little_data", "days": len(series)}
    recent = series[-RECENT_DAYS:]
    ref_part = series[-REF_DAYS:-GUARD_DAYS]
    means = [_mean([x[1] for x in ref_part[i:i + 28]]) for i in range(0, max(1, len(ref_part) - 27))]
    ref = _pctl(means, REF_PCTL)
    now_i, now_c = _mean([x[1] for x in recent]), _mean([x[2] for x in recent])
    out = {"days": len(series), "end": series[-1][0], "recent_impr_per_day": round(now_i, 1),
           "recent_clicks_per_day": round(now_c, 2), "ref_impr_per_day": round(ref, 1),
           "ratio": round(now_i / ref, 3) if ref else None, "step": step_down(series)}
    if ref < min_ref:
        out["state"] = "never_visible" if now_i < min_ref else "growing"
    elif now_i / ref < COLLAPSED:
        out["state"] = "collapsed"
    elif now_i / ref < SEVERE:
        out["state"] = "severe_drop"
    elif now_i / ref < DECLINING:
        out["state"] = "declining"
    else:
        out["state"] = "growing" if now_i / ref > 1.25 else "stable"
    return out


def page_breadth(db, site):
    """How many pages Google showed at all in the last 90 days vs the last 14 (Search Analytics by page)."""
    r = db.execute("""SELECT SUM(CASE WHEN gsc_impr > 0 THEN 1 ELSE 0 END), SUM(CASE WHEN gsc_recent_impr > 0 THEN 1 ELSE 0 END)
                      FROM perf WHERE site=? AND gsc_at IS NOT NULL""", (site,)).fetchone()
    return {"pages_90d": r[0] or 0, "pages_14d": r[1] or 0}


def console_links(prop):
    if not prop:
        return {}
    rid = quote(prop, safe="")
    return {"manual_actions": f"https://search.google.com/search-console/manual-actions?resource_id={rid}",
            "security_issues": f"https://search.google.com/search-console/security-issues?resource_id={rid}",
            "pages_report": f"https://search.google.com/search-console/index?resource_id={rid}"}


def visibility(db, site, meta=None, counts=None):
    """{google, bing: engine verdict, breadth, links, state (the worse of the two that can be judged), headline, steps}."""
    meta = meta or {}
    out = {src: engine_visibility(_daily(db, site, src)) for src in ENGINE}
    out["breadth"] = page_breadth(db, site)
    out["links"] = console_links(meta.get("gsc_property"))
    rank = {"collapsed": 0, "severe_drop": 1, "declining": 2, "stable": 3, "growing": 4, "never_visible": 5, "too_little_data": 6}
    judged = [(rank[out[s]["state"]], s) for s in ENGINE if out[s]["state"] in rank]
    worst = min(judged)[1] if judged else None
    out["state"] = out[worst]["state"] if worst else "too_little_data"
    out["engine"] = worst
    out["headline"], out["steps"] = _advice(site, out, counts or Counter())
    return out


def _advice(site, v, counts):
    g, b = v["google"], v["bing"]
    bits, steps = [], []
    for src, e in (("google", g), ("bing", b)):
        if e["state"] in ("collapsed", "severe_drop", "declining"):
            step = e.get("step")
            when = (f"; the biggest step down began around {step['day']} ({step['before']:.0f} to {step['after']:.0f} impressions/day)"
                    if step else "; a gradual slide, no single step")
            bits.append(f"{ENGINE[src]} now shows the site {e['recent_impr_per_day']:.0f} times a day against its usual "
                        f"{e['ref_impr_per_day']:.0f} ({e['ratio']:.0%} of normal){when}")
    br = v["breadth"]
    if g["state"] in ("collapsed", "severe_drop") and br["pages_90d"]:
        bits.append(f"pages Google showed at all: {br['pages_14d']} in the last 14 days against {br['pages_90d']} in 90 days")
    if not bits:
        return "", []
    links = v["links"]
    if g["state"] in ("collapsed", "severe_drop"):
        steps.append({"what": "Rule out a manual action and a security issue in Search Console (no API reports either; it takes one minute).",
                      "href": links.get("manual_actions"), "cta": "Manual actions"})
        steps.append({"what": "Check Search Console's Page indexing report for a jump in 'Excluded by noindex', 'Blocked by robots.txt', "
                              "'Server error (5xx)' or 'Duplicate, Google chose different canonical' starting near the step date: "
                              "a technical block shows there, a quality judgment does not.",
                      "href": links.get("pages_report"), "cta": "Page indexing"})
        blocked = sum(n for k, n in counts.items() if k in BLOCKERS)
        total = sum(counts.values())
        if total and blocked / total >= 0.2:
            steps.append({"what": f"{blocked} of {total} known URLs on this site are blocked right now (noindex, redirect, canonical "
                                  "elsewhere, robots or errors). Check that this is intended before anything else."})
        steps.append({"what": "If neither shows a cause, treat it as a site-wide quality judgment: stop requesting indexing for this site "
                              "(it cannot help), work through the prune plan below one batch at a time, and fix the flagged pages that "
                              "still earn impressions first.", "href": f"#/recovery?site={site}", "cta": "Prune plan"})
    elif b["state"] in ("collapsed", "severe_drop"):
        steps.append({"what": "Open Bing Webmaster Tools > Site Explorer and URL Inspection for a sample of lost pages; check Bing's crawl "
                              "report (Revival page) for robots blocks or server errors around the step date.",
                      "href": "https://www.bing.com/webmasters", "cta": "Bing Webmaster"})
    return ". ".join(b[0].upper() + b[1:] for b in bits) + ".", steps


# ---------------------------------------------------------------- template artifacts (scaled-content signals)

HEADING = re.compile(r"<h([2-4])[^>]*>(.*?)</h\1>", re.I | re.S)
TAG = re.compile(r"<[^>]+>")
# Text an automated pipeline leaves behind and a reader should never see.
# High precision only: phrases that never belong in a published article. (No "placeholder" or "{{...}}": a prompt site
# uses both legitimately.)
LEFTOVERS = re.compile(r"wordpress media library|image generation failed|as an ai language model|\[insert [^\]]{1,40}\]|"
                       r"lorem ipsum dolor|\[(?:image|citation|source) needed\]", re.I)
BOILER_SHARE, BOILER_MIN = 0.25, 5    # a heading on this share of posts (and at least this many) is site boilerplate
NUMBERED_RUN = 3                      # this many headings on one page that differ only by a number are a template...
NUMBERED_MIN_WORDS = 4                # ... when they say more than "Week N" or "Day N workout" (training plans are fine)
# Section names every editor uses on many posts; repeating them is normal, not scaffolding.
GENERIC = {"faq", "faqs", "frequently asked questions", "conclusion", "final thoughts", "key takeaways", "table of contents",
           "summary", "related posts", "related articles", "references", "sources", "introduction", "overview",
           "pros and cons", "bottom line", "the bottom line", "verdict", "our verdict", "about the author", "you may also like",
           "further reading", "takeaways", "what you need", "how we chose", "methodology", "disclaimer", "wrap-up", "wrapping up",
           "next steps", "pros", "cons", "specs", "specifications", "comments", "share this", "in this article"}


def _text(fragment):
    return re.sub(r"\s+", " ", html.unescape(TAG.sub(" ", fragment or ""))).strip()


def _shape(heading):
    return re.sub(r"#?\d+", "#", heading.lower())


def template_scan(db, site):
    """Scaled-content signals in the stored WordPress content: headings repeated on a large share of the site's posts,
    runs of headings that differ only by a number ("... #1", "... #2", ...), and text an automated pipeline left behind.
    Returns {"pages": {url: [findings]}, "boilerplate": [(heading, posts)], "scanned": n}. Read-only."""
    heads, found = {}, defaultdict(list)
    for url, raw in db.execute("SELECT url, raw FROM link_corpus WHERE site=?", (site,)):
        try:
            text = zlib.decompress(raw).decode("utf-8", "replace")
        except (zlib.error, TypeError):
            continue
        hs = [_text(m.group(2)) for m in HEADING.finditer(text)]
        hs = [x for x in hs if 3 <= len(x) <= 160]
        heads[url] = hs
        shapes = Counter(_shape(x) for x in hs if re.search(r"\d", x))
        for shape, n in shapes.items():
            if n >= NUMBERED_RUN and len(re.findall(r"[a-z]+", shape)) >= NUMBERED_MIN_WORDS:
                found[url].append(f"{n} headings follow one numbered template (“{shape.replace('#', 'N')}”)")
        for x in hs:
            if LEFTOVERS.search(x):
                found[url].append(f"automation leftover in a heading: “{x[:90]}”")
        body = _text(text)
        m = LEFTOVERS.search(body)
        if m and not any("leftover" in f for f in found[url]):
            found[url].append(f"automation leftover in the text: “…{body[max(0, m.start() - 30):m.end() + 40].strip()}…”")
    n = len(heads)
    norm_h = lambda x: re.sub(r"[^\w\s&-]", "", x.lower()).strip()  # noqa: E731
    per = Counter(h for hs in heads.values() for h in {norm_h(x) for x in hs})
    floor = max(BOILER_MIN, BOILER_SHARE * n)
    boiler = sorted(((h, k) for h, k in per.items() if k >= floor and h not in GENERIC
                     and len(re.findall(r"[^\W\d_]{3,}", h)) >= 2),
                    key=lambda x: -x[1])
    bset = {h for h, _ in boiler}
    for url, hs in heads.items():
        mine = sorted({norm_h(x) for x in hs} & bset)
        if len(mine) >= 2:
            found[url].append(f"{len(mine)} headings repeated across the site (“{mine[0][:60]}”, …)")
    return {"pages": {u: f for u, f in found.items() if f}, "boilerplate": boiler[:15], "scanned": n}


def template_message(scan):
    """Site-level diagnosis line, or None."""
    if not scan or not scan["scanned"]:
        return None
    k = len(scan["pages"])
    if k < max(3, 0.1 * scan["scanned"]):
        return None
    top = "; ".join(f"“{h[:50]}” on {n}" for h, n in scan["boilerplate"][:3])
    return ("site", f"{k} of {scan['scanned']} posts carry signs of templated, automated content (flag TEMPLATE_ARTIFACT): numbered "
                    "template headings, pipeline leftovers or the same headings repeated across posts"
                    + (f" ({top})" if top else "") + ". This is what Google's scaled-content policy targets. Remove the "
                    "leftovers and the repeated scaffolding, worst pages first; never replace them with invented detail.")


# ---------------------------------------------------------------- prune plan

def _closest(url, title, candidates):
    """The most related page among candidates (url, title, strength) by shared title words; None when nothing shares two."""
    mine = tokens(clean_title(title))
    best = None
    for u, t, strength in candidates:
        if u == url:
            continue
        shared = len(mine & tokens(clean_title(t)))
        if shared >= 2 and (best is None or (shared, strength) > best[0]):
            best = ((shared, strength), u, t)
    return (best[1], best[2]) if best else (None, None)


def _home(url):
    return urlsplit(url).path in ("", "/")


def prune_plan(db, site, buckets, dups, inbound=None, artifacts=None):
    """One decision per indexable URL of the site, with the evidence behind it. buckets: {url: (bucket, flags)} from triage;
    dups: {url: (other, overlap)}; inbound: {url: external inbound links} or None when unknown."""
    rows = db.execute("""SELECT u.url, u.kind, u.lastmod, u.first_seen, u.published, a.title, a.words,
                                COALESCE(p.gsc_clicks, 0) AS gc, COALESCE(p.gsc_impr, 0) AS gi,
                                COALESCE(p.bing_clicks, 0) AS bc, COALESCE(p.bing_impr, 0) AS bi,
                                COALESCE(t.g_prev, 0) + COALESCE(t.b_prev, 0) AS prev_clicks
                         FROM urls u LEFT JOIN audits a ON a.url=u.url LEFT JOIN perf p ON p.url=u.url
                         LEFT JOIN page_trend t ON t.url=u.url WHERE u.site=?""", (site,)).fetchall()
    live = [r for r in rows if r["url"] in buckets and buckets[r["url"]][0] not in BLOCKERS
            and "IGNORED" not in buckets[r["url"]][1]]
    strength = {r["url"]: 5 * (r["gc"] + r["bc"]) + 0.05 * (r["gi"] + r["bi"]) for r in live}
    indexed = [(r["url"], r["title"] or "", strength[r["url"]]) for r in live
               if buckets[r["url"]][0] == "INDEXED" or r["gc"] + r["bc"] > 0]
    out = []
    for r in live:
        url = r["url"]
        bucket, flags = buckets[url]
        flags = set(flags) | ({"TEMPLATE_ARTIFACT"} if (artifacts or {}).get(url) else set())
        clicks, impr = r["gc"] + r["bc"] + r["prev_clicks"], max(r["gi"], 0) + max(r["bi"], 0)
        age = days_since(r["published"] or r["first_seen"])
        links = (inbound or {}).get(url)
        ev = {"url": url, "title": r["title"] or "", "kind": r["kind"] or "", "bucket": bucket, "flags": ",".join(sorted(flags)),
              "google_clicks_90d": r["gc"], "google_impr_90d": r["gi"], "bing_clicks": r["bc"], "bing_impr": r["bi"],
              "words": r["words"] or 0, "age_days": round(age) if age is not None else None,
              "inbound_links": links if inbound is not None else None,
              "artifacts": (artifacts or {}).get(url, [])[:3]}
        dup = dups.get(url)
        quality_flags = flags & {"TRUST_RISK", "THIN", "SOFT_404", "NEAR_DUPLICATE", "TEMPLATE_ARTIFACT"}
        if _home(url) or (r["kind"] or "") in HUB_KINDS:
            d, why = "keep", "Home page or hub: it carries the site's structure."
        elif clicks > 0 or impr >= KEEP_IMPR:
            if quality_flags - {"NEAR_DUPLICATE"}:
                d, why = "fix", (f"Earns search traffic ({clicks} clicks, {impr:,} impressions) but is flagged "
                                 f"{', '.join(sorted(quality_flags - {'NEAR_DUPLICATE'})).lower().replace('_', ' ')}: fix this page first, "
                                 "it is one Google still shows.")
            else:
                d, why = "keep", f"Earns search traffic: {clicks} clicks, {impr:,} impressions."
        elif dup and strength.get(dup[0], -1) >= strength[url] and dup[0] in strength:
            d, why = "merge", (f"Shares about {dup[1]:.0%} of its main text with a page that is as strong or stronger. Move anything "
                               "unique into that page, then 301 this URL to it.")
            ev["target"] = dup[0]
        elif impr >= DEMAND_IMPR and quality_flags:
            d, why = "fix", (f"Some search demand ({impr} impressions, no clicks) and flagged "
                             f"{', '.join(sorted(quality_flags)).lower().replace('_', ' ')}: fix it before deciding.")
        elif age is not None and age < MIN_AGE_DAYS:
            d, why = "review", f"Only {age:.0f} days old: too new to judge by search data."
        elif impr < DEMAND_IMPR and (quality_flags or bucket in NOT_INDEXED):
            reason = ([f"Google: {bucket.replace('_', ' ').lower()}"] if bucket in NOT_INDEXED else []) + \
                     [f.replace("_", " ").lower() for f in sorted(quality_flags)]
            if links:
                tgt, tt = _closest(url, r["title"], indexed)
                d = "redirect" if tgt else "review"
                why = (f"No search value (0 clicks, {impr} impressions; {', '.join(reason)}) but {links} external links point at it. "
                       + ("301 it to the closest page Google shows, so the links keep counting." if tgt else
                          "No closely related indexed page to redirect to: decide by hand."))
                if tgt:
                    ev["target"] = tgt
            else:
                d, why = "noindex", (f"No search value on either engine (0 clicks, {impr} impressions) and {', '.join(reason)}. "
                                     "Noindex it (reversible) so Google judges the site by its better pages.")
        else:
            d, why = "review", (f"No clicks and {impr} impressions, but nothing else counts against it"
                                f"{' (indexed by Google)' if bucket == 'INDEXED' else ''}. Owner's call: improve, merge or keep.")
        out.append({**ev, "decision": d, "why": why})
    order = {d: i for i, d in enumerate(("fix", "noindex", "merge", "redirect", "review", "keep"))}
    out.sort(key=lambda x: (order[x["decision"]], -(x["google_impr_90d"] + x["bing_impr"]), x["url"]))
    return out


def summarize(plan):
    c = Counter(x["decision"] for x in plan)
    return {d: c.get(d, 0) for d in DECISIONS}


def needs_recovery(vis, diagnosis):
    """A site needs the prune plan when Google's visibility collapsed or fell hard, or triage judged the whole site."""
    if (vis or {}).get("google", {}).get("state") in ("collapsed", "severe_drop"):
        return True
    return any(k == "site" and "judgment about the whole site" in m for k, m in diagnosis or [])


def inbound_links(db, site):
    """{url: external links} from Bing's inbound-link report, or None when it has never been read for this site."""
    rows = db.execute("SELECT url, links FROM inbound_links WHERE site=?", (site,)).fetchall()
    return {r[0]: r[1] for r in rows} if rows else None


def analyze(db, site, res):
    vis = visibility(db, site, res.get("meta"), res.get("counts"))
    scan = template_scan(db, site)
    inbound = inbound_links(db, site)
    plan = prune_plan(db, site, res["buckets"], res.get("dups") or {}, inbound, scan["pages"])
    msg = template_message(scan)
    if msg and msg not in res.get("diagnosis", []):
        res.setdefault("diagnosis", []).insert(0, msg)
    return {"visibility": vis, "active": needs_recovery(vis, res.get("diagnosis")) or bool(msg), "plan": plan,
            "totals": summarize(plan), "inbound_known": inbound is not None,
            "templates": {"pages": len(scan["pages"]), "scanned": scan["scanned"], "boilerplate": scan["boilerplate"][:10]}}


def site_summary(rec):
    if not rec:
        return None
    v = rec["visibility"]
    return {"state": v["state"], "engine": v.get("engine"), "headline": v["headline"], "steps": v["steps"], "links": v["links"],
            "google": v["google"], "bing": v["bing"], "breadth": v["breadth"], "active": rec["active"], "totals": rec["totals"],
            "inbound_known": rec["inbound_known"], "templates": rec.get("templates")}
