# -*- coding: utf-8 -*-
"""Measured results of the links the engine applied, and what the engine learns from them.

For every page that received an internal link from the engine, at least 31 days ago:
  * a page that already ranked ("growth"): its Google clicks, impressions and average position over the 28 days
    before the first link and the 28 days after, against similar pages of the same site that got no link in those
    weeks (the controls: the pages closest to its impressions before). The effect is the target's change minus
    the controls' median change (difference in differences), so a seasonal or site-wide swing cancels out. Where the
    link's anchor is a search the page shows for, that search's position before and after is measured too.
  * a page that was not indexed ("index"): whether Google indexed it within 30 days of the link, against the pages
    of the same site that were not indexed at the same time and got no link (their share indexed in the same 30 days).
Two Search Analytics calls per measurement window serve every page of a property (cached per run); one more call per
growth page reads its searches. Each page is measured once.

What the engine learns: once enough pages are measured, the measured effect scales how strongly each kind of link is
ranked against the other (calibration(): a multiplier between 0.5 and 1.5, 1 while the sample is small). These are
observational comparisons on the engine's own sites, not randomized experiments: a positive result is evidence, not
proof. The dashboard says so.
"""
import datetime as dt
import json
import math
import statistics
from collections import defaultdict

from .google import state_of
from .util import days_since, log, norm, now_iso, parse_ts

WINDOW = 28
MIN_AGE = WINDOW + 3            # Google's data trails by 2-3 days
MAX_AGE = 400                   # Search Analytics keeps about 16 months
PER_RUN = 40                    # pages measured per daily run
CONTROLS = 5
MIN_CALIBRATION = 15            # measured pages of a kind before its result changes how links are ranked
INDEX_DAYS = 30

SCHEMA = """CREATE TABLE IF NOT EXISTS results(target TEXT PRIMARY KEY, site TEXT, kind TEXT, applied_at TEXT, links INT,
  anchor TEXT, measured_at TEXT, data TEXT)"""


def _day(ts):
    t = parse_ts(ts)
    return t.date() if t else None


def _window(first_day):
    """((before_start, before_end), (after_start, after_end)) around the day of the first link."""
    return ((first_day - dt.timedelta(days=WINDOW), first_day - dt.timedelta(days=1)),
            (first_day + dt.timedelta(days=1), first_day + dt.timedelta(days=WINDOW)))


def _state_at(history, when):
    """Google state of a page at a moment, from its inspection history [(time, state)] (oldest first)."""
    state = None
    for t, s in history:
        if t and t <= when:
            state = s
    return state


def _indexed_between(history, start, end):
    return any(t and start < t <= end and s == "indexed" for t, s in history)


def _pages(client, prop, start, end, cache):
    """{url: (clicks, impressions, position)} for every page of the property in a date range (cached per run)."""
    key = (prop, start, end)
    if key not in cache:
        out = {}
        for r in client.search_pages(prop, start, end):
            u = norm(r["keys"][0])
            if u:
                c, i, p = out.get(u, (0, 0, 0.0))
                ri = int(r.get("impressions", 0))
                pos = float(r.get("position") or 0) * ri
                out[u] = (c + int(r.get("clicks", 0)), i + ri, p + pos)
        cache[key] = {u: (c, i, (p / i) if i else None) for u, (c, i, p) in out.items()}
    return cache[key]


def _log_change(before, after):
    return math.log((after + 1) / (before + 1))


def _controls(target, before, after, linked, n=CONTROLS):
    """Pages with no link in the window, closest to the target's impressions before (and seen before at all)."""
    base = before.get(target, (0, 0, None))[1]
    pool = [u for u, (_c, i, _p) in before.items() if u != target and u not in linked and i > 0]
    pool.sort(key=lambda u: abs(math.log((before[u][1] + 1) / (base + 1))))
    return [(u, before[u], after.get(u, (0, 0, None))) for u in pool[:n]]


def measure_growth(target, anchor, before, after, linked, anchor_rows=None):
    """Difference-in-differences for one page that already ranked. Pure function (tested directly)."""
    b, a = before.get(target, (0, 0, None)), after.get(target, (0, 0, None))
    ctrl = _controls(target, before, after, linked)
    if not ctrl:
        return None
    med = lambda xs: statistics.median(xs) if xs else 0.0  # noqa: E731
    c_clicks = med([_log_change(cb[0], ca[0]) for _u, cb, ca in ctrl])
    c_impr = med([_log_change(cb[1], ca[1]) for _u, cb, ca in ctrl])
    c_pos = med([ca[2] - cb[2] for _u, cb, ca in ctrl if ca[2] is not None and cb[2] is not None])
    out = {"before": {"clicks": b[0], "impr": b[1], "pos": round(b[2], 1) if b[2] else None},
           "after": {"clicks": a[0], "impr": a[1], "pos": round(a[2], 1) if a[2] else None},
           "controls": [u for u, _b, _a in ctrl],
           "clicks_lift": round(math.exp(_log_change(b[0], a[0]) - c_clicks) - 1, 3),
           "impr_lift": round(math.exp(_log_change(b[1], a[1]) - c_impr) - 1, 3),
           "pos_change": round((a[2] - b[2]) - c_pos, 1) if a[2] is not None and b[2] is not None else None}
    if anchor_rows:
        q = (anchor or "").lower().strip()
        qb, qa = anchor_rows.get("before", {}).get(q), anchor_rows.get("after", {}).get(q)
        if qb or qa:
            out["anchor_query"] = {"query": q, "pos_before": qb and round(qb[2], 1), "pos_after": qa and round(qa[2], 1),
                                   "impr_before": qb[1] if qb else 0, "impr_after": qa[1] if qa else 0}
    return out


def _history(db):
    hist = defaultdict(list)
    for r in db.execute("SELECT url, inspected_at, coverage FROM gsc_history ORDER BY url, inspected_at"):
        hist[r["url"]].append((parse_ts(r["inspected_at"]), state_of(r["coverage"])))
    return hist


def measure_index(db, site, target, first, hist, linked_any):
    """Indexed within INDEX_DAYS of the link, against same-site pages not indexed then and never linked."""
    end = first + dt.timedelta(days=INDEX_DAYS)
    got = _indexed_between(hist.get(target, []), first, end)
    peers = [r["url"] for r in db.execute("SELECT url FROM urls WHERE site=?", (site,))
             if r["url"] != target and r["url"] not in linked_any]
    pool = [u for u in peers if _state_at(hist.get(u, []), first) in ("crawled", "discovered", "unknown")]
    if not pool:
        return None
    rate = sum(_indexed_between(hist.get(u, []), first, end) for u in pool) / len(pool)
    return {"indexed": got, "control_rate": round(rate, 3), "controls": len(pool),
            "checked_after": any(t and t > first for t, _s in hist.get(target, []))}


def due(db):
    """Pages to measure: first applied link at least MIN_AGE days ago, not measured yet."""
    db.execute(SCHEMA)
    rows = db.execute("""SELECT l.target, l.site, MIN(l.applied_at) AS first, COUNT(*) AS n,
                                (SELECT anchor FROM link_ops x WHERE x.target=l.target AND x.status='applied'
                                 ORDER BY x.applied_at LIMIT 1) AS anchor
                         FROM link_ops l LEFT JOIN results r ON r.target=l.target
                         WHERE l.status='applied' AND l.applied_at IS NOT NULL AND r.target IS NULL
                         GROUP BY l.target, l.site""").fetchall()
    return [r for r in rows if MIN_AGE <= (days_since(r["first"]) or 0) <= MAX_AGE]


def measure(db, client, props, max_pages=PER_RUN):
    """Measure due pages with Google data. Returns the number measured. Read-only towards Google and the sites."""
    todo = [r for r in due(db) if r["site"] in props][:max_pages]
    if not todo:
        return 0
    hist = _history(db)
    all_links = db.execute("SELECT target, applied_at FROM link_ops WHERE status IN ('applied', 'undone')").fetchall()
    linked_any = {r["target"] for r in all_links}
    cache, done = {}, 0
    for r in todo:
        first_t = parse_ts(r["first"])
        first = first_t.date()
        (b0, b1), (a0, a1) = _window(first)
        linked = {x["target"] for x in all_links
                  if (_day(x["applied_at"]) or first) >= b0 and (_day(x["applied_at"]) or first) <= a1}
        kind = "growth" if _state_at(hist.get(r["target"], []), first_t) == "indexed" else "index"
        try:
            if kind == "index" and _state_at(hist.get(r["target"], []), first_t) is None:
                before = _pages(client, props[r["site"]], b0, b1, cache)
                kind = "growth" if before.get(r["target"], (0, 0))[1] > 0 else "index"
            if kind == "growth":
                before = _pages(client, props[r["site"]], b0, b1, cache)
                after = _pages(client, props[r["site"]], a0, a1, cache)
                anchor_rows = None
                if hasattr(client, "search_page_queries"):
                    anchor_rows = {"before": client.search_page_queries(props[r["site"]], r["target"], b0, b1),
                                   "after": client.search_page_queries(props[r["site"]], r["target"], a0, a1)}
                data = measure_growth(r["target"], r["anchor"], before, after, linked, anchor_rows)
            else:
                data = measure_index(db, r["site"], r["target"], first_t, hist, linked_any)
        except Exception as e:
            log(f"[results] {r['site']}: measuring {r['target']} failed: {type(e).__name__}: {str(e)[:120]}")
            continue
        if data is None:
            continue
        db.execute("INSERT OR REPLACE INTO results VALUES(?,?,?,?,?,?,?,?)",
                   (r["target"], r["site"], kind, r["first"], r["n"], r["anchor"], now_iso(), json.dumps(data)))
        done += 1
    db.commit()
    log(f"[results] measured {done} linked pages ({len(todo) - done} could not be measured yet)")
    return done


def _sign_p(wins, n):
    """Two-sided sign test: chance of a split at least this uneven if links made no difference."""
    if not n:
        return None
    k = max(wins, n - wins)
    tail = sum(math.comb(n, i) for i in range(k, n + 1)) / 2 ** n
    return round(min(1.0, 2 * tail), 4)


def summary(db):
    """What the measured links did, per kind, for the dashboard and for calibration()."""
    db.execute(SCHEMA)
    rows = [dict(r, data=json.loads(r["data"])) for r in db.execute("SELECT * FROM results ORDER BY measured_at DESC")]
    growth = [r for r in rows if r["kind"] == "growth"]
    index = [r for r in rows if r["kind"] == "index"]
    out = {"growth": None, "index": None, "pages": [
        {k: r[k] for k in ("target", "site", "kind", "applied_at", "links", "anchor")} | {"data": r["data"]} for r in rows[:300]]}
    if growth:
        lifts = [r["data"]["clicks_lift"] for r in growth]
        imprs = [r["data"]["impr_lift"] for r in growth]
        pos = [r["data"]["pos_change"] for r in growth if r["data"].get("pos_change") is not None]
        wins = sum(1 for x in imprs if x > 0)
        out["growth"] = {"n": len(growth), "median_clicks_lift": round(statistics.median(lifts), 3),
                         "median_impr_lift": round(statistics.median(imprs), 3),
                         "median_pos_change": round(statistics.median(pos), 1) if pos else None,
                         "beat_controls": wins, "p_value": _sign_p(wins, len(imprs))}
    if index:
        measured = [r for r in index if r["data"].get("checked_after")]
        if measured:
            rate = sum(r["data"]["indexed"] for r in measured) / len(measured)
            ctrl = statistics.mean(r["data"]["control_rate"] for r in measured)
            out["index"] = {"n": len(measured), "indexed_rate": round(rate, 3), "control_rate": round(ctrl, 3),
                            "lift": round(rate - ctrl, 3)}
    return out


def calibration(db, s=None):
    """{kind: multiplier} for ranking links: what the engine learned from its own measured results."""
    s = s or summary(db)
    out = {"growth": 1.0, "index": 1.0}
    g = s.get("growth")
    if g and g["n"] >= MIN_CALIBRATION:
        out["growth"] = 1.5 if g["median_impr_lift"] > 0.1 else 0.5 if g["median_impr_lift"] <= 0 else 1.0
    i = s.get("index")
    if i and i["n"] >= MIN_CALIBRATION:
        out["index"] = 1.5 if i["lift"] > 0.1 else 0.5 if i["lift"] <= 0 else 1.0
    return out
