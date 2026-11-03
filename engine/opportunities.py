# -*- coding: utf-8 -*-
"""Search opportunities: where a page already shows for a search and a specific, checkable change is most likely to pay.

Three kinds, all from the engines' own query data (engine/queries.py), never from guesses:
  close_to_top  the page ranks around 4-15 for a search with real impressions. The estimated gain is what the site's own
                click-through at position 3 would bring, minus what the page already gets.
  low_ctr       the page already ranks in the top ~5 but is clicked far less than the site's usual click-through at that
                position, and the shortfall is too large to be chance (binomial test). The search snippet (title, meta
                description) is the usual cause.
  split         two or more of the site's own pages share one search's impressions, none of them in the top 3. Engines
                rotating between near-equal pages is a common reason none ranks well.
Click-through by position is learned per site and engine from its own data, shrunk toward the portfolio's (so a site with
little data borrows strength), and forced to never rise as the position gets worse. Estimates are per 4 weeks.
"""
import math
import re
from collections import Counter, defaultdict
from urllib.parse import urlsplit

from . import config, queries
from .triage import BLOCKERS

MAX_POS = 20
PRIOR_IMPR = 300            # how many impressions' worth of portfolio data a site's own curve starts from
MIN_IMPR = 30               # impressions in 4 weeks before a page/search pair is considered at all
MIN_IMPR_CTR = 80           # ... before a low click-through is judged
MIN_GAIN = 1.0              # estimated clicks per 4 weeks worth listing
CTR_SHORTFALL = 0.5         # low_ctr: clicked at less than half the usual rate for its position
P_CHANCE = 0.02             # ... and less than a 2% chance of doing that by luck
SPLIT_SHARE = 0.25          # split: every page counted holds at least a quarter of the search's impressions
SPLIT_MIN_IMPR = 50
TOP_ITEMS = 60
ENGINE = {"bing": "Bing", "google": "Google"}


# ---------------------------------------------------------------- click-through by position

def _bucket(pos):
    return min(MAX_POS, max(1, int(round(pos or MAX_POS))))


def _pava_decreasing(values, weights):
    """Weighted least-squares fit that never increases (pool adjacent violators)."""
    blocks = [[v, w, 1] for v, w in zip(values, weights)]
    i = 0
    while i < len(blocks) - 1:
        if blocks[i][0] < blocks[i + 1][0]:
            v1, w1, n1 = blocks[i]
            v2, w2, n2 = blocks[i + 1]
            w = w1 + w2
            blocks[i] = [(v1 * w1 + v2 * w2) / w if w else (v1 + v2) / 2, w, n1 + n2]
            del blocks[i + 1]
            i = max(0, i - 1)
        else:
            i += 1
    out = []
    for v, _, n in blocks:
        out.extend([v] * n)
    return out


def ctr_curve(rows, prior=None, k=PRIOR_IMPR):
    """Click-through rate for positions 1..20 from (clicks, impressions, position) rows; index 0 is position 1."""
    clicks, impr = [0] * MAX_POS, [0] * MAX_POS
    for c, i, p in rows:
        b = _bucket(p) - 1
        clicks[b] += c or 0
        impr[b] += i or 0
    if prior:
        rate = [(clicks[b] + k * prior[b]) / (impr[b] + k) for b in range(MAX_POS)]
        weight = [impr[b] + k for b in range(MAX_POS)]
    else:
        known = [b for b in range(MAX_POS) if impr[b]]
        if not known:
            return [0.0] * MAX_POS
        rate = []
        for b in range(MAX_POS):
            if impr[b]:
                rate.append(clicks[b] / impr[b])
            else:                                            # no data at this position: borrow the nearest that has some
                rate.append(clicks[min(known, key=lambda x: abs(x - b))] / impr[min(known, key=lambda x: abs(x - b))])
        weight = [max(1, impr[b]) for b in range(MAX_POS)]
    return [round(max(0.0, v), 5) for v in _pava_decreasing(rate, weight)]


def at(curve, pos):
    return curve[_bucket(pos) - 1]


def p_at_most(k, n, p):
    """P(X <= k) for X ~ Binomial(n, p), computed in log space (exact; n is a few thousand at most here)."""
    if p <= 0:
        return 1.0
    if p >= 1:
        return 0.0 if k < n else 1.0
    lp, lq = math.log(p), math.log1p(-p)
    total = 0.0
    for j in range(0, min(k, n) + 1):
        total += math.exp(math.lgamma(n + 1) - math.lgamma(j + 1) - math.lgamma(n - j + 1) + j * lp + (n - j) * lq)
    return min(1.0, total)


# ---------------------------------------------------------------- analysis

def _squash(text):
    return "".join(ch for ch in (text or "").lower() if ch.isalnum())


def brands(db, site):
    """What people type when they search for the site itself: the domain's name and the name the site's titles end with
    ("... | GearUpToFit"), when most titles share one. Searches for a site by name are not ranking opportunities."""
    host = urlsplit(config.site_base(site)).hostname or ""
    out = {_squash(host.split(".")[0])} if not host.replace(".", "").isdigit() else set()
    titles = [r[0] for r in db.execute("SELECT a.title FROM audits a JOIN urls u ON u.url=a.url WHERE u.site=? AND a.title != ''",
                                       (site,))]
    tails = Counter(_squash(re.split(r"\s[|–—-]\s", t)[-1]) for t in titles if re.search(r"\s[|–—-]\s", t))
    if tails:
        tail, n = tails.most_common(1)[0]
        if n >= 0.3 * len(titles):
            out.add(tail)
    return {b for b in out if len(b) >= 5}


def _branded(query, brand_set):
    q = _squash(query)
    return any(b in q for b in brand_set)


def _skip(query, brand_set):
    """Searches that are not content opportunities: the site's own name, and searches with no letters at all
    ("17-8", "2+2"), which are almost always arithmetic or codes rather than a topic the page can answer better."""
    return _branded(query, brand_set) or not any(ch.isalpha() for ch in query)


def portfolio_curves(db):
    out = {}
    for src in ENGINE:
        rows = db.execute("SELECT clicks, impr, pos FROM page_queries WHERE src=? AND pos IS NOT NULL", (src,)).fetchall()
        out[src] = ctr_curve([(r[0], r[1], r[2]) for r in rows]) if rows else None
    return out


def _desc_state(desc):
    if desc is None or not desc.strip():
        return "missing"
    return f"{len(desc)} characters"


def analyze(db, site, buckets, prior=None):
    """Opportunities for one site. buckets: {url: (bucket, flags)} from triage, so blocked or ignored pages are skipped."""
    prior = prior if prior is not None else portfolio_curves(db)
    brand = brands(db, site)
    rows = db.execute("""SELECT q.src, q.url, q.query, q.clicks, q.impr, q.pos, a.title, a.description
                         FROM page_queries q LEFT JOIN audits a ON a.url=q.url
                         WHERE q.site=? AND q.pos IS NOT NULL""", (site,)).fetchall()
    live = [r for r in rows if r["url"] in buckets and buckets[r["url"]][0] not in BLOCKERS
            and "IGNORED" not in buckets[r["url"]][1]]
    curves = {}
    for src in ENGINE:
        own = [(r["clicks"], r["impr"], r["pos"]) for r in live if r["src"] == src]
        if own:
            curves[src] = ctr_curve(own, prior=prior.get(src))
    items = []
    for r in live:
        q, i, c, pos, src = r["query"], r["impr"], r["clicks"], r["pos"], r["src"]
        if i < MIN_IMPR or _skip(q, brand) or src not in curves:
            continue
        curve, ctr = curves[src], c / i
        here = at(curve, pos)
        base = {"src": src, "url": r["url"], "title": r["title"], "query": q, "pos": round(pos, 1), "impr": i, "clicks": c,
                "ctr": round(ctr, 4)}
        if 3.5 < pos <= 15:
            target = curve[2]
            gain = i * max(0.0, target - max(ctr, here))
            if gain >= MIN_GAIN:
                items.append({**base, "kind": "close_to_top", "expected_ctr": round(here, 4), "target_ctr": round(target, 4),
                              "gain": round(gain, 1),
                              "advice": (f"Shown on {ENGINE[src]} for “{q}” at about position {pos:.0f} ({i:,} times in 4 weeks, "
                                         f"{c} clicks). In the top 3 it would get an estimated +{gain:.0f} clicks per 4 weeks, judging by "
                                         "your own site's click-through by position. Make the page answer this search plainly near the "
                                         "top, cover what the searcher needs that it lacks (with named, checkable sources), and link to "
                                         "it from related pages that already rank.")})
        elif pos <= 5.5 and i >= MIN_IMPR_CTR and ctr < CTR_SHORTFALL * here:
            chance = p_at_most(c, i, here)
            if chance < P_CHANCE:
                gain = i * (here - ctr)
                if gain >= MIN_GAIN:
                    title = (r["title"] or "").strip()
                    items.append({**base, "kind": "low_ctr", "expected_ctr": round(here, 4), "gain": round(gain, 1),
                                  "chance": round(chance, 5), "title_len": len(title), "description": _desc_state(r["description"]),
                                  "advice": (f"Already near the top on {ENGINE[src]} for “{q}” (position {pos:.1f}) but clicked "
                                             f"{ctr:.1%} of the time, against {here:.1%} usual at that position on your site. The search "
                                             f"snippet is the likely problem: rewrite the title (now “{title[:90]}”, {len(title)} "
                                             f"characters) and the meta description ({_desc_state(r['description'])}) so they say plainly "
                                             f"what the page gives this searcher. Estimated +{gain:.0f} clicks per 4 weeks.")})
    by_query = defaultdict(list)
    for r in live:
        if r["impr"] >= 15 and not _skip(r["query"], brand):
            by_query[(r["src"], r["query"])].append(r)
    for (src, q), rs in by_query.items():
        total = sum(x["impr"] for x in rs)
        shared = [x for x in rs if x["impr"] >= SPLIT_SHARE * total]
        if len(shared) >= 2 and total >= SPLIT_MIN_IMPR and min(x["pos"] for x in shared) > 3:
            shared.sort(key=lambda x: -x["impr"])
            items.append({"kind": "split", "src": src, "query": q, "impr": total, "gain": None,
                          "pages": [{"url": x["url"], "title": x["title"], "impr": x["impr"], "pos": round(x["pos"], 1),
                                     "clicks": x["clicks"]} for x in shared],
                          "advice": (f"{len(shared)} of your pages share the {ENGINE[src]} impressions for “{q}” ({total:,} in 4 weeks) "
                                     "and none ranks in the top 3. Engines often rotate between near-equal pages instead of ranking one "
                                     "well. Pick one main page for this search; make the others clearly about something else, or merge "
                                     "them into it after checking clicks on both engines.")})
    pages = by_page([x for x in items if x["gain"] is not None])
    splits = sorted([x for x in items if x["kind"] == "split"], key=lambda x: -x["impr"])
    totals = {k: sum(1 for x in pages if x["kind"] == k) for k in ("close_to_top", "low_ctr")}
    totals["split"] = len(splits)
    totals["searches"] = sum(len(x["queries"]) for x in pages)
    totals["gain"] = round(sum(x["gain"] for x in pages), 1)
    return {"items": pages[:TOP_ITEMS] + splits[:20], "totals": totals, "curves": curves, "coverage": queries.coverage(db, site),
            "pairs": len(live)}


def by_page(found):
    """One item per page and kind: the owner improves a page once, not once per wording of the same search. The page's
    gain is the sum over its searches; its advice is built on the biggest one."""
    groups = defaultdict(list)
    for x in found:
        groups[(x["kind"], x["src"], x["url"])].append(x)
    out = []
    for (kind, src, url), xs in groups.items():
        xs.sort(key=lambda x: -x["gain"])
        lead = xs[0]
        gain = round(sum(x["gain"] for x in xs), 1)
        more = f" It also shows for {len(xs) - 1} related search{'es' if len(xs) > 2 else ''} listed here." if len(xs) > 1 else ""
        advice = lead["advice"].replace(f"+{lead['gain']:.0f} clicks", f"+{gain:.0f} clicks (all its searches together)") if len(xs) > 1 else lead["advice"]
        out.append({**{k: lead.get(k) for k in ("kind", "src", "url", "title", "query", "pos", "impr", "clicks", "ctr", "expected_ctr",
                                                 "target_ctr", "title_len", "description")},
                    "gain": gain, "impr_total": sum(x["impr"] for x in xs), "advice": advice + more,
                    "queries": [{k: x[k] for k in ("query", "pos", "impr", "clicks", "ctr", "gain")} for x in xs[:8]]})
    return sorted(out, key=lambda x: -x["gain"])


def site_summary(opp):
    """The compact per-site view for the dashboard summary."""
    if not opp:
        return None
    top = [{k: x.get(k) for k in ("kind", "src", "url", "query", "pos", "impr", "gain")} for x in opp["items"][:3]]
    return {**opp["totals"], "coverage": opp.get("coverage"), "pairs": opp.get("pairs", 0), "top": top}
