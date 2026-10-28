# -*- coding: utf-8 -*-
"""Search traffic and Bing index health over time.

Indexation is a means; clicks are the goal. This module keeps the daily history the engines already publish
(Bing: clicks/impressions and its own crawl report; Google: Search Analytics by date) and answers, with the
evidence stated: is each site's traffic rising or falling, is that change statistically clear, and is Bing's
index of the site healthy.

Honesty rules: windows end on the last day the engine reported (not "today"), a change is called real only when
it is large AND clear against the noise of count data, and everything says what it covers.
"""
import datetime as dt
import math

from .bing import bing_date
from .util import days_since, log, now_iso

SPAN = 28                  # days per comparison window
KEEP_DAYS = 540
MIN_CLICKS = 50            # below this in both windows a percentage change is noise
MIN_CHANGE = 0.25          # relative change worth reporting
Z_LIMIT = 3.0              # standard errors a difference must clear


def windows(end):
    """((now_start, now_end), (prev_start, prev_end)) as ISO dates: the latest SPAN days ending on `end`, and the SPAN before."""
    e = dt.date.fromisoformat(str(end)[:10])
    n0, p1 = e - dt.timedelta(days=SPAN - 1), e - dt.timedelta(days=SPAN)
    p0 = e - dt.timedelta(days=2 * SPAN - 1)
    return (n0.isoformat(), e.isoformat()), (p0.isoformat(), p1.isoformat())


def series_end(db, site, src):
    row = db.execute("SELECT MAX(day) FROM traffic_daily WHERE site=? AND src=?", (site, src)).fetchone()
    return row[0] if row and row[0] else None


def compare(now, prev):
    """Is the change between two windows of daily counts real? Counts differ by chance by about sqrt(a + b), so a
    change must be at least MIN_CHANGE of the earlier value AND more than Z_LIMIT such units. Daily traffic is noisier
    than a pure count, so 'medium' confidence is indicative, not proof."""
    delta = now - prev
    pct = None if not prev else round(delta / prev * 100, 1)
    if max(now, prev) < MIN_CLICKS:
        return {"verdict": "too_little_data", "pct": pct, "z": None, "confidence": None}
    z = delta / math.sqrt(now + prev)
    big = abs(delta) / max(prev, 1) >= MIN_CHANGE
    verdict = "declining" if z <= -Z_LIMIT and big else "growing" if z >= Z_LIMIT and big else "stable"
    conf = None if verdict == "stable" else "high" if abs(z) >= 6 else "medium"
    return {"verdict": verdict, "pct": pct, "z": round(z, 1), "confidence": conf}


def aliaser(db, site):
    """(alias, seen): alias(url) maps a spelling variant (trailing slash added or removed) onto the URL the engine lists;
    `seen` collects {variant: listed} for every mapping made. The engines often report a page under the other spelling,
    and without this its clicks never reach the page they belong to."""
    known = {r[0] for r in db.execute("SELECT url FROM urls WHERE site=?", (site,))}
    seen = {}

    def alias(u):
        if u in known:
            return u
        for cand in (u + "/", u.rstrip("/"), u.rstrip("/") + "/"):
            if cand != u and cand in known:
                seen[u] = cand
                return cand
        return u
    return alias, seen


def save_variants(db, site, seen):
    for variant, listed in seen.items():
        db.execute("INSERT OR REPLACE INTO url_variants(variant, site, listed) VALUES(?,?,?)", (variant, site, listed))


# ---------------------------------------------------------------- pulling

def _store_series(db, site, src, points):
    n = 0
    for day, clicks, impr in points:
        if day:
            db.execute("INSERT OR REPLACE INTO traffic_daily(site, day, src, clicks, impr) VALUES(?,?,?,?,?)",
                       (site, day, src, int(round(clicks or 0)), int(round(impr or 0))))
            n += 1
    return n


def _store_crawl(db, site, rows):
    n = 0
    for r in rows:
        day = bing_date(r.get("Date"))
        if not day:
            continue
        g = lambda k: int(r.get(k) or 0)  # noqa: E731
        db.execute("""INSERT OR REPLACE INTO bing_crawl(site, day, crawled, in_index, errors, c4xx, c5xx, robots, timeouts, dns, in_links)
                      VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                   (site, day, g("CrawledPages"), g("InIndex"), g("CrawlErrors"), g("Code4xx"), g("Code5xx"),
                    g("BlockedByRobotsTxt"), g("ConnectionTimeout"), g("DnsFailures"), g("InLinks")))
        n += 1
    return n


def pull(db, gclient, props, bclient, bsites, today=None):
    """Refresh the daily histories. Bing returns its whole history in one call; Google is read for the last 16 months
    the first time and for the latest 60 days afterwards (the engines revise recent days)."""
    today = today or dt.date.today()
    for site, burl in (bsites or {}).items():
        try:
            series = bclient.rank_traffic(burl)
            crawl = bclient.crawl_stats(burl)
        except Exception as e:
            log(f"[traffic] {site}: Bing statistics failed: {str(e)[:120]}")
            continue
        n = _store_series(db, site, "bing", [(bing_date(r.get("Date")), r.get("Clicks"), r.get("Impressions")) for r in series])
        m = _store_crawl(db, site, crawl)
        db.commit()
        log(f"[traffic] {site}: Bing {n} days of traffic, {m} days of crawl statistics")
    for site, prop in (props or {}).items():
        last = series_end(db, site, "google")
        start = today - dt.timedelta(days=480) if not last else dt.date.fromisoformat(last) - dt.timedelta(days=60)
        try:
            rows = gclient.search_dates(prop, start, today - dt.timedelta(days=2))
        except Exception as e:
            log(f"[traffic] {site}: Google traffic failed: {str(e)[:120]}")
            continue
        n = _store_series(db, site, "google", [(r["keys"][0], r.get("clicks"), r.get("impressions")) for r in rows])
        db.commit()
        log(f"[traffic] {site}: Google {n} days of traffic")
    cutoff = (today - dt.timedelta(days=KEEP_DAYS)).isoformat()
    db.execute("DELETE FROM traffic_daily WHERE day < ?", (cutoff,))
    db.execute("DELETE FROM bing_crawl WHERE day < ?", (cutoff,))
    db.commit()


# ---------------------------------------------------------------- reading

def site_traffic(db, site, series_days=120):
    """{bing, google: window comparison or None, series: dense daily rows [day, bing clicks, bing impr, google clicks, google impr]}."""
    out = {}
    for src in ("bing", "google"):
        end = series_end(db, site, src)
        if not end:
            out[src] = None
            continue
        (n0, n1), (p0, p1) = windows(end)
        q = "SELECT COALESCE(SUM(clicks),0), COALESCE(SUM(impr),0), COUNT(*) FROM traffic_daily WHERE site=? AND src=? AND day BETWEEN ? AND ?"
        cn, inow, dn = db.execute(q, (site, src, n0, n1)).fetchone()
        cp, ip, dp = db.execute(q, (site, src, p0, p1)).fetchone()
        first = db.execute("SELECT MIN(day) FROM traffic_daily WHERE site=? AND src=?", (site, src)).fetchone()[0]
        hist = (dt.date.fromisoformat(end) - dt.date.fromisoformat(first)).days + 1
        clicks, impressions = compare(cn, cp), compare(inow, ip)
        if hist < 2 * SPAN:        # the earlier window would be partial: never call a trend from it
            for v in (clicks, impressions):
                v.update(verdict="too_little_data", z=None, confidence=None, reason=f"only {hist} days of history; 56 are needed")
        out[src] = {"end": end, "now": [n0, n1], "prev": [p0, p1], "clicks_now": cn, "clicks_prev": cp, "impr_now": inow,
                    "impr_prev": ip, "clicks": clicks, "impressions": impressions, "history_days": hist,
                    "days_reported_now": dn, "days_reported_prev": dp, "enough_history": hist >= 2 * SPAN}
    ends = [out[s]["end"] for s in ("bing", "google") if out.get(s)]
    series = []
    if ends:
        last = dt.date.fromisoformat(max(ends))
        rows = {}
        for r in db.execute("SELECT day, src, clicks, impr FROM traffic_daily WHERE site=? AND day >= ?",
                            (site, (last - dt.timedelta(days=series_days - 1)).isoformat())):
            rows[(r["day"], r["src"])] = (r["clicks"], r["impr"])
        for k in range(series_days - 1, -1, -1):
            d = (last - dt.timedelta(days=k)).isoformat()
            b, g = rows.get((d, "bing"), (0, 0)), rows.get((d, "google"), (0, 0))
            series.append([d, b[0], b[1], g[0], g[1]])
    out["series"] = series
    return out


def bing_health(db, site):
    """Bing's own crawl report for the site, with the findings that deserve a look. None when Bing reports nothing."""
    latest = db.execute("SELECT * FROM bing_crawl WHERE site=? ORDER BY day DESC LIMIT 1", (site,)).fetchone()
    if not latest:
        return None
    old = db.execute("SELECT * FROM bing_crawl WHERE site=? AND day <= ? ORDER BY day DESC LIMIT 1",
                     (site, (dt.date.fromisoformat(latest["day"]) - dt.timedelta(days=30)).isoformat())).fetchone()
    wk = db.execute("""SELECT SUM(crawled) c, SUM(errors) e, SUM(c4xx) x4, SUM(c5xx) x5, SUM(robots) r, SUM(timeouts) t, SUM(dns) d,
                              COUNT(*) n FROM bing_crawl WHERE site=? AND day > ?""",
                    (site, (dt.date.fromisoformat(latest["day"]) - dt.timedelta(days=7)).isoformat())).fetchone()
    known = db.execute("SELECT COUNT(*) FROM urls WHERE site=? AND (in_sitemap=1 OR in_wp=1)", (site,)).fetchone()[0]
    in_index = latest["in_index"]
    ratio = round(in_index / known, 1) if known else None
    growth = round((in_index - old["in_index"]) / old["in_index"] * 100, 1) if old and old["in_index"] else None
    crawled, errors, x4, x5, robots, tmo, dns = (wk[k] or 0 for k in ("c", "e", "x4", "x5", "r", "t", "d"))
    server = x5 + tmo + dns
    findings = []
    if in_index >= 1000 and ratio and ratio >= 5:
        findings.append({"kind": "index_bloat", "severity": "warn",
                         "message": f"Bing reports {in_index:,} pages in its index for a site with {known:,} URLs the engine can find ({ratio:.0f} times as many)"
                                    + (f", up {growth:.0f}% in 30 days" if growth and growth >= 15 else "")
                                    + ". That often means duplicate, parameter or archive URLs are indexed, which can dilute crawling. "
                                      "Check Bing Webmaster Tools: Index Explorer, then robots.txt, canonical tags and URL-parameter settings."})
    if robots >= 300 and robots / max(1, crawled + robots) >= 0.25:
        findings.append({"kind": "robots_blocked", "severity": "warn",
                         "message": f"{robots / (crawled + robots) * 100:.0f}% of the URLs Bing tried last week ({robots:,}) were blocked by robots.txt. "
                                    "Bing keeps finding URLs your robots.txt disallows: find out which (often search or parameter URLs) and stop linking to them."})
    if server >= 20 and crawled and server / crawled >= 0.01:
        findings.append({"kind": "server_errors", "severity": "warn",
                         "message": f"Bing met {server:,} server errors, timeouts or DNS failures last week ({server / crawled * 100:.1f}% of {crawled:,} pages it crawled). "
                                    "Repeated failures make Bing crawl a site less: check hosting capacity and error logs."})
    if x4 >= 50 and crawled and x4 / crawled >= 0.05:
        findings.append({"kind": "missing_pages", "severity": "info",
                         "message": f"Bing hit {x4:,} missing pages (4xx) last week ({x4 / crawled * 100:.0f}% of {crawled:,} crawled): "
                                    "links or sitemaps still point at removed URLs. Redirect or remove those links."})
    return {"day": latest["day"], "in_index": in_index, "in_index_30d_ago": old["in_index"] if old else None, "growth_pct": growth,
            "known_urls": known, "ratio": ratio, "crawled_week": crawled, "errors_week": errors, "c4xx_week": x4,
            "c5xx_week": x5, "robots_week": robots, "timeouts_week": tmo, "dns_week": dns, "in_links": latest["in_links"],
            "findings": findings}


def data_health(db, site):
    """How much to trust the numbers: how many indexable pages have a Google verdict and how old those verdicts are."""
    from .triage import BLOCKERS
    marks = ",".join("?" * len(BLOCKERS))
    indexable = db.execute(f"SELECT COUNT(*) FROM triage WHERE site=? AND bucket NOT IN ({marks}, 'NOT_CHECKED')",
                           (site, *sorted(BLOCKERS))).fetchone()[0]
    ages = [r[0] for r in db.execute("""SELECT g.inspected_at FROM gsc g JOIN triage t ON t.url=g.url
                                        WHERE t.site=? AND g.inspected_at IS NOT NULL""", (site,))]
    days = sorted(d for d in (days_since(a) for a in ages) if d is not None)
    unverified = db.execute("SELECT COUNT(*) FROM triage WHERE site=? AND bucket IN ('NOT_INSPECTED')", (site,)).fetchone()[0]
    return {"indexable": indexable, "unverified": unverified,
            "verdict_share": round(1 - unverified / indexable, 3) if indexable else None,
            "median_verdict_age_days": round(days[len(days) // 2], 1) if days else None, "at": now_iso()}
