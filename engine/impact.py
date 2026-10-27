# -*- coding: utf-8 -*-
"""Observed indexing transitions and status summaries for past actions.

These figures are associations, not causal effects. Request and link status may predate the action;
Bing impressions may also predate submission. Missing verdicts remain in the totals.
"""
import datetime as dt
from collections import defaultdict

from .google import state_of
from .util import parse_ts

NONIDX = {"crawled", "discovered", "unknown"}


def _ago(days):
    return dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days)


def request_outcomes(db, min_age_days=7):
    rows = db.execute("""SELECT o.url, t.bucket FROM owner_state o LEFT JOIN triage t ON t.url=o.url
                         WHERE o.requested_at IS NOT NULL AND julianday('now') - julianday(o.requested_at) >= ?""",
                      (min_age_days,)).fetchall()
    return len(rows), sum(1 for r in rows if r["bucket"] == "INDEXED")


def request_rate(db):
    """Measured share of manual requests that ended indexed, shrunk toward 10% while the sample is small."""
    n, s = request_outcomes(db)
    return (s + 1) / (n + 10)


def compute(db, days=30):
    since = _ago(days)
    hist = defaultdict(list)
    for r in db.execute("SELECT url, inspected_at, coverage FROM gsc_history ORDER BY url, inspected_at"):
        hist[r["url"]].append((parse_ts(r["inspected_at"]), state_of(r["coverage"])))
    gained, lost = set(), set()
    for url, h in hist.items():
        for (_, s0), (t1, s1) in zip(h, h[1:]):
            if t1 and t1 >= since:
                if s0 in NONIDX and s1 == "indexed":
                    gained.add(url)
                elif s0 == "indexed" and s1 in NONIDX:
                    lost.add(url)
    rn, rs = request_outcomes(db)
    links = db.execute("""SELECT l.target, t.bucket FROM link_ops l LEFT JOIN triage t ON t.url=l.target
                          WHERE l.status='applied' AND julianday('now') - julianday(l.applied_at) >= 7""").fetchall()
    subs = db.execute("""SELECT s.url, MIN(s.submitted_at) AS first, b.last_crawled, COALESCE(p.bing_impr,0) AS impr
                         FROM submissions s LEFT JOIN bing_info b ON b.url=s.url LEFT JOIN perf p ON p.url=s.url
                         WHERE s.http_status BETWEEN 200 AND 299 GROUP BY s.url
                         HAVING julianday('now') - julianday(MIN(s.submitted_at)) >= 3""").fetchall()
    crawled = sum(1 for r in subs if r["impr"] or (r["last_crawled"] and r["last_crawled"] >= (r["first"] or "")[:10]))
    return {"days": days, "gained": len(gained), "lost": len(lost), "gained_urls": sorted(gained)[:50],
            "lost_urls": sorted(lost)[:50],
            "requests": {"measured": rn, "indexed": rs, "rate_used": round(request_rate(db), 3)},
            "links": {"measured": len(links), "indexed": sum(1 for r in links if r["bucket"] == "INDEXED")},
            "bing": {"measured": len(subs), "crawled_or_shown": crawled}}
