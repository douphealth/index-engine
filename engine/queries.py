# -*- coding: utf-8 -*-
"""What each page is shown for in search: the queries behind its impressions, per engine, over the latest 28 days.

Google gives every (query, page) pair of a property in one call, so the daily run takes it whole. Bing gives the queries
of one page per call (about two seconds each), so its pages are refreshed a few at a time on idle ticks and in the daily
run, highest-impression pages first, each at most once a week. Read-only: nothing is submitted or changed anywhere.
"""
import datetime as dt
import time

from . import traffic
from .bing import bing_date
from .triage import BLOCKERS
from .util import days_since, log, norm, now_iso

WINDOW_DAYS = 28
REFRESH_DAYS = 7            # a page's Bing queries are re-read at most once a week
KEEP_PER_PAGE = 150         # queries kept per page (by impressions); the long tail adds rows, not decisions
GOOGLE_LAG_DAYS = 3         # Search Console's final data trails by about three days
PAUSE_S = 0.3
SITE_FAIL_LIMIT = 3


def _window(end):
    end = dt.date.fromisoformat(str(end)[:10])
    return (end - dt.timedelta(days=WINDOW_DAYS - 1)).isoformat(), end.isoformat()


def _store(db, src, site, url, agg, win_end):
    db.execute("DELETE FROM page_queries WHERE src=? AND url=?", (src, url))
    best = sorted(agg.items(), key=lambda kv: -kv[1][1])[:KEEP_PER_PAGE]
    db.executemany("INSERT INTO page_queries(src, url, site, query, clicks, impr, pos, win_end) VALUES(?,?,?,?,?,?,?,?)",
                   [(src, url, site, q, c, i, round(p / i, 2) if i else None, win_end) for q, (c, i, p) in best])


def pull_google(db, client, props, today=None):
    """One Search Analytics call per property: every (query, page) of the last 28 days of final data."""
    today = today or dt.date.today()
    start, end = _window(today - dt.timedelta(days=GOOGLE_LAG_DAYS))
    n_all = 0
    for site, prop in props.items():
        try:
            rows = client.search_queries(prop, start, end)
        except Exception as e:
            log(f"[queries] {site}: Search Console query report failed: {str(e)[:120]}")
            continue
        alias, seen = traffic.aliaser(db, site)
        pages = {}
        for r in rows:
            q, page = (r.get("keys") or [None, None])[:2]
            if not q or not page:
                continue
            u = alias(norm(page))
            c, i, p = pages.setdefault(u, {}).get(q, (0, 0, 0.0))
            impr = int(r.get("impressions") or 0)
            pages[u][q] = (c + int(r.get("clicks") or 0), i + impr, p + float(r.get("position") or 0) * impr)
        traffic.save_variants(db, site, seen)
        db.execute("DELETE FROM page_queries WHERE src='google' AND site=?", (site,))
        for u, agg in pages.items():
            _store(db, "google", site, u, agg, end)
        db.commit()
        n_all += len(rows)
        log(f"[queries] {site}: Google {len(rows)} query/page pairs on {len(pages)} pages ({start} to {end})")
    return n_all


def bing_due(db, site, max_per_site):
    """Pages whose Bing queries are worth (re)reading: listed, not blocked, shown on Bing, not read in the last week;
    highest Bing impressions first, never-read pages before stale ones."""
    rows = db.execute(f"""SELECT p.url, p.bing_impr, f.fetched_at FROM perf p JOIN urls u ON u.url=p.url
                          LEFT JOIN triage t ON t.url=p.url LEFT JOIN query_fetch f ON f.src='bing' AND f.url=p.url
                          WHERE p.site=? AND COALESCE(p.bing_impr, 0) > 0
                            AND COALESCE(t.bucket, '') NOT IN ({','.join('?' * len(BLOCKERS))})""",
                      (site, *BLOCKERS)).fetchall()
    due = [r for r in rows if r["fetched_at"] is None or (days_since(r["fetched_at"]) or 0) >= REFRESH_DAYS]
    due.sort(key=lambda r: (r["fetched_at"] is not None, -(r["bing_impr"] or 0)))
    return [r["url"] for r in due[:max_per_site]]


def pull_bing(db, client, bsites, max_total=60, max_per_site=40):
    """Read the queries of a few pages per site, taking turns between sites, and stop at the first throttle."""
    queues = {site: (burl, bing_due(db, site, max_per_site)) for site, burl in bsites.items()}
    queues = {s: q for s, q in queues.items() if q[1]}
    done, fails, total, throttled = {}, {}, 0, None
    while queues and total < max_total and not throttled:
        for site in list(queues):
            burl, urls = queues[site]
            if not urls:
                del queues[site]
                continue
            url = urls.pop(0)
            try:
                rows = client.page_query_stats(burl, url)
            except Exception as e:
                if "throttle" in str(e).lower():
                    throttled = str(e)[-70:]
                    break
                fails[site] = fails.get(site, 0) + 1
                log(f"[queries] {site}: Bing page queries failed for {url}: {str(e)[:100]}")
                if fails[site] >= SITE_FAIL_LIMIT:
                    del queues[site]
                continue
            fails[site] = 0
            dated = [(bing_date(r.get("Date")), r) for r in rows]
            dated = [(d, r) for d, r in dated if d]
            end = traffic.series_end(db, site, "bing") or max((d for d, _ in dated), default=None)
            agg = {}
            if end:
                start, end = _window(end)
                for d, r in dated:
                    if not (start <= d <= end):
                        continue
                    q = (r.get("Query") or "").strip().lower()
                    impr = int(r.get("Impressions") or 0)
                    if not q or not impr:
                        continue
                    c, i, p = agg.get(q, (0, 0, 0.0))
                    agg[q] = (c + int(r.get("Clicks") or 0), i + impr, p + float(r.get("AvgImpressionPosition") or 0) * impr)
                _store(db, "bing", site, url, agg, end)
            db.execute("INSERT OR REPLACE INTO query_fetch(src, url, site, fetched_at, rows) VALUES('bing',?,?,?,?)",
                       (url, site, now_iso(), len(agg)))
            done[site] = done.get(site, 0) + 1
            total += 1
            if total >= max_total:
                break
            if PAUSE_S:
                time.sleep(PAUSE_S)
    db.commit()
    for site, n in done.items():
        left = len(queues.get(site, (None, []))[1])
        log(f"[queries] {site}: Bing queries read for {n} pages" + (f", {left} more due" if left else ""))
    if throttled:
        log(f"[queries] Bing throttled after {total} page reads ({throttled.strip()}): the rest waits for the next tick")
    return total


def coverage(db, site):
    """How much of the site's Bing impressions the stored page queries cover (0-1), and how many pages are read."""
    tot = db.execute("SELECT COALESCE(SUM(bing_impr), 0) FROM perf WHERE site=?", (site,)).fetchone()[0] or 0
    got = db.execute("""SELECT COALESCE(SUM(p.bing_impr), 0), COUNT(*) FROM query_fetch f JOIN perf p ON p.url=f.url
                        WHERE f.src='bing' AND f.site=?""", (site,)).fetchone()
    return {"bing_pages_read": got[1], "bing_impr_covered": round(got[0] / tot, 3) if tot else None}
