# -*- coding: utf-8 -*-
"""Crawler telemetry: which pages Googlebot and Bingbot fetched, day by day, and what the sites answered them.

Read from Cloudflare's own analytics for each site's zone (GraphQL httpRequestsAdaptiveGroups), so nothing is added in
front of the sites and nothing on them changes. Cloudflare keeps about 31 days of it. The token needs only
"Analytics Read" (Index-Engine-Cloudflare-Analytics-Token.txt); the engine never writes anything with it.

What it is good for:
  * within a day of a change (a link, a cleanup, a noindex) the engine sees whether Googlebot came back to the page,
    instead of waiting weeks for Search Console;
  * a site that answers crawlers with errors (5xx, 429) is caught the next morning, before Search Console reports it;
  * a page Googlebot has not fetched for 30 days is a page Google does not care about, which is evidence for triage.

Limits, stated where the numbers are shown: crawlers are recognised by their user agent (the free plan has no verified
bot field), so a fake "Googlebot" counts too, and Cloudflare samples busy zones ("adaptive"), so counts are estimates.
Days are UTC. A site whose DNS is not on a Cloudflare account the token can read has no telemetry.
"""
import datetime as dt
import os
from collections import defaultdict
from pathlib import Path
from urllib.parse import urlsplit

import requests

from . import config
from .util import SECRETS, fetched, log, now_iso

API = "https://api.cloudflare.com/client/v4"
TOKEN_FILES = [Path(p) for p in os.environ.get("INDEX_ENGINE_ANALYTICS_TOKEN_FILES", "").split(os.pathsep) if p] or [
    config.PORTFOLIO / "Index-Engine-Cloudflare-Analytics-Token.txt"]
BOTS = {"google": "%Googlebot%", "bing": "%bingbot%"}
KEEP_DAYS = 30                  # Cloudflare keeps 4 weeks and 3 days; a day is read once it is complete
ROW_LIMIT = 10000               # rows per query: a day of one zone has far fewer distinct paths and statuses
ERROR_SHARE = 0.10              # this share of a day's Googlebot fetches answered 5xx or 429 is an alert...
ERROR_MIN_HITS = 20             # ... when Googlebot fetched at least this many pages that day
MISSING_SHARE = 0.15            # this share of a week's Googlebot fetches answered 404/410 is crawl spent on nothing...
MISSING_MIN_HITS = 100          # ... worth a warning when the week had at least this many fetches

SCHEMA = """
CREATE TABLE IF NOT EXISTS crawl_hits(site TEXT, day TEXT, bot TEXT, path TEXT, status INT, hits INT,
  PRIMARY KEY(site, day, bot, path, status));
CREATE TABLE IF NOT EXISTS crawl_days(site TEXT, day TEXT, read_at TEXT, rows INT, truncated INT DEFAULT 0,
  PRIMARY KEY(site, day));
CREATE INDEX IF NOT EXISTS crawl_hits_path ON crawl_hits(site, path);
"""

SPAN_DAYS = 7                   # days read per query (one call reads both crawlers); a full span is split in half
Q = """query($zone: String!, $since: Time!, $until: Time!, $host: String!) { viewer { zones(filter: {zoneTag: $zone}) {
  %s } } }""" % " ".join(
    f"""{bot}: httpRequestsAdaptiveGroups(limit: {ROW_LIMIT}, filter: {{datetime_geq: $since, datetime_lt: $until,
    clientRequestHTTPHost: $host, userAgent_like: "{ua}"}}) {{ count dimensions {{ date clientRequestPath edgeResponseStatus }} }}"""
    for bot, ua in BOTS.items())


def tokens():
    """Analytics tokens from the configured files (comment lines start with #). Added to SECRETS, never printed."""
    from .onboard import TOKEN_RE
    out = []
    for path in TOKEN_FILES:
        try:
            out += TOKEN_RE.findall(path.read_text(encoding="utf-8", errors="replace"))
        except OSError:
            continue
    out = list(dict.fromkeys(out))
    SECRETS.extend(out)
    return out


def _host(site):
    return urlsplit(config.site_base(site)).hostname or site


def zones(toks, session=None):
    """{domain: (token, zone id)} for every zone the tokens can read."""
    s = session or requests
    out = {}
    for t in toks:
        try:
            r = s.get(f"{API}/zones", headers={"Authorization": f"Bearer {t}"}, params={"per_page": 50}, timeout=30)
            for z in (r.json().get("result") or []) if r.status_code == 200 else []:
                out.setdefault(z["name"], (t, z["id"]))
        except (requests.RequestException, ValueError):
            continue
    return out


def zone_for(site, zmap):
    """The zone that serves the site's host: the longest zone name the host ends with."""
    host = _host(site)
    best = None
    for name, z in zmap.items():
        if host == name or host.endswith("." + name):
            if best is None or len(name) > len(best[0]):
                best = (name, z)
    return best[1] if best else None


def due_days(db, site, today=None, keep=KEEP_DAYS):
    """Complete UTC days not read yet, oldest first (at most `keep` days back)."""
    today = today or dt.datetime.now(dt.timezone.utc).date()
    have = {r[0] for r in db.execute("SELECT day FROM crawl_days WHERE site=?", (site,))}
    days = [today - dt.timedelta(days=i) for i in range(keep, 0, -1)]
    return [d.isoformat() for d in days if d.isoformat() not in have]


def spans(days, size=SPAN_DAYS):
    """Consecutive runs of ISO days, at most `size` long: one query each. Pure (tested)."""
    out, cur = [], []
    for d in sorted(days):
        if cur and (dt.date.fromisoformat(d) - dt.date.fromisoformat(cur[-1])).days != 1 or len(cur) >= size:
            out.append(cur)
            cur = []
        cur.append(d)
    return out + ([cur] if cur else [])


def read_span(token, zone, host, days, session=None):
    """{day: {(bot, path, status): hits}} for one site over consecutive UTC days (every day present, even with no hits),
    and whether any day may be incomplete. A span that fills the row limit is read again in halves."""
    s = session or requests
    start = dt.datetime.fromisoformat(days[0]).replace(tzinfo=dt.timezone.utc)
    iso = lambda t: t.isoformat().replace("+00:00", "Z")  # noqa: E731
    r = s.post(f"{API}/graphql", headers={"Authorization": f"Bearer {token}"}, timeout=90,
               json={"query": Q, "variables": {"zone": zone, "host": host, "since": iso(start),
                                               "until": iso(start + dt.timedelta(days=len(days)))}})
    body = r.json()
    if body.get("errors"):
        raise RuntimeError(str(body["errors"][0].get("message", ""))[:200])
    zones_ = body["data"]["viewer"]["zones"]
    got = zones_[0] if zones_ else {}
    full = any(len(got.get(bot) or []) >= ROW_LIMIT for bot in BOTS)
    if full and len(days) > 1:
        half = len(days) // 2
        a, ta = read_span(token, zone, host, days[:half], session)
        b, tb = read_span(token, zone, host, days[half:], session)
        return {**a, **b}, ta or tb
    out = {d: {} for d in days}
    for bot in BOTS:
        for g in got.get(bot) or []:
            dim = g["dimensions"]
            day = out.setdefault(dim["date"], {})
            key = (bot, dim["clientRequestPath"] or "/", int(dim["edgeResponseStatus"] or 0))
            day[key] = day.get(key, 0) + int(g["count"])
    return out, full


def pull(db, sites, session=None, today=None, max_days=KEEP_DAYS):
    """Read the complete days not read yet for every site with a readable zone. Read-only towards Cloudflare.
    Returns {site: days read}. Several sites are read at once; the database is written in order (util.fetched)."""
    db.executescript(SCHEMA)
    toks = tokens()
    if not toks:
        return {}
    zmap = zones(toks, session)
    todo = []
    for site in sites:
        z = zone_for(site, zmap)
        days = due_days(db, site, today, max_days) if z else []
        if days:
            todo.append((site, z, days))

    def read(item):
        site, (tok, zid), days = item
        got = []
        for span in spans(days):
            by_day, truncated = read_span(tok, zid, _host(site), span, session)
            got += [(day, by_day.get(day) or {}, truncated) for day in span]
        return got

    out = {}
    for (site, _z, days), got, err in fetched(todo, read):
        if err:
            log(f"[crawl] {site}: Cloudflare analytics could not be read ({str(err)[:140]})")
            continue
        for day, hits, truncated in got:
            db.execute("DELETE FROM crawl_hits WHERE site=? AND day=?", (site, day))
            db.executemany("INSERT INTO crawl_hits VALUES(?,?,?,?,?,?)",
                           [(site, day, bot, path, status, n) for (bot, path, status), n in hits.items()])
            db.execute("INSERT OR REPLACE INTO crawl_days VALUES(?,?,?,?,?)", (site, day, now_iso(), len(hits), int(truncated)))
        out[site] = len(got)
    db.execute("DELETE FROM crawl_hits WHERE day < ?", ((dt.date.today() - dt.timedelta(days=KEEP_DAYS + 5)).isoformat(),))
    db.execute("DELETE FROM crawl_days WHERE day < ?", ((dt.date.today() - dt.timedelta(days=KEEP_DAYS + 5)).isoformat(),))
    db.commit()
    if out:
        log(f"[crawl] crawler telemetry read for {len(out)} sites ({sum(out.values())} site-days)")
    return out


def _path(url):
    p = urlsplit(url).path or "/"
    return p


def pages(db, site):
    """{url: {"g_last", "g_7d", "g_30d", "b_last", "b_30d", "g_errors_7d"}} for the site's known URLs that a crawler
    fetched in the kept days. A URL is matched by its path, with and without the trailing slash."""
    db.executescript(SCHEMA)
    last = db.execute("SELECT MAX(day) FROM crawl_days WHERE site=?", (site,)).fetchone()[0]
    if not last:
        return {}
    week = (dt.date.fromisoformat(last) - dt.timedelta(days=6)).isoformat()
    by_path = defaultdict(lambda: {"g_last": None, "g_7d": 0, "g_30d": 0, "b_last": None, "b_30d": 0, "g_errors_7d": 0})
    for r in db.execute("SELECT day, bot, path, status, hits FROM crawl_hits WHERE site=?", (site,)):
        p = r["path"].rstrip("/") or "/"
        x = by_path[p]
        if r["bot"] == "google":
            x["g_30d"] += r["hits"]
            x["g_last"] = max(x["g_last"] or "", r["day"])
            if r["day"] >= week:
                x["g_7d"] += r["hits"]
                x["g_errors_7d"] += r["hits"] if r["status"] >= 500 or r["status"] == 429 else 0
        else:
            x["b_30d"] += r["hits"]
            x["b_last"] = max(x["b_last"] or "", r["day"])
    out = {}
    for r in db.execute("SELECT url FROM urls WHERE site=?", (site,)):
        p = _path(r["url"]).rstrip("/") or "/"
        if p in by_path:
            out[r["url"]] = dict(by_path[p])
    return out


def site_summary(db, site):
    """Per-day crawler totals and what the site answered Googlebot, for the dashboard. None without telemetry."""
    db.executescript(SCHEMA)
    days = [r[0] for r in db.execute("SELECT day FROM crawl_days WHERE site=? ORDER BY day", (site,))]
    if not days:
        return None
    series = defaultdict(lambda: [0, 0, 0])       # day -> [googlebot hits, googlebot 5xx/429, bingbot hits]
    status = defaultdict(int)
    week = (dt.date.fromisoformat(days[-1]) - dt.timedelta(days=6)).isoformat()
    for r in db.execute("SELECT day, bot, status, SUM(hits) AS n FROM crawl_hits WHERE site=? GROUP BY day, bot, status", (site,)):
        s = series[r["day"]]
        if r["bot"] == "google":
            s[0] += r["n"]
            s[1] += r["n"] if r["status"] >= 500 or r["status"] == 429 else 0
            if r["day"] >= week:
                status[str(r["status"])] += r["n"]
        else:
            s[2] += r["n"]
    known = pages(db, site)
    n_urls = db.execute("SELECT COUNT(*) FROM urls WHERE site=?", (site,)).fetchone()[0]
    g7 = sum(series[d][0] for d in days if d >= week)
    errors = [{"path": r["path"], "status": r["status"], "hits": r["n"]} for r in db.execute(
        """SELECT path, status, SUM(hits) AS n FROM crawl_hits WHERE site=? AND bot='google' AND day>=? AND status>=400
           GROUP BY path, status ORDER BY n DESC LIMIT 10""", (site, week))]
    return {"days": len(days), "last_day": days[-1], "series": [[d, *series[d]] for d in days],
            "googlebot_7d": g7, "bingbot_7d": sum(series[d][2] for d in days if d >= week),
            "status_7d": dict(status), "errors_7d": errors,
            "pages_fetched_30d": sum(1 for x in known.values() if x["g_30d"]), "pages_known": n_urls,
            "truncated": bool(db.execute("SELECT 1 FROM crawl_days WHERE site=? AND truncated=1", (site,)).fetchone())}


def after_changes(db, days=30):
    """For the engine's own changes of the last `days` days (applied links, cleanups, noindex/redirects): did Googlebot
    fetch the changed page again after the change? {kind: {"n", "refetched", "pending"}} plus the pages."""
    db.executescript(SCHEMA)
    rows = []
    for kind, sql in (("link", "SELECT site, target AS url, MIN(applied_at) AS at FROM link_ops WHERE status='applied' GROUP BY site, target"),
                      ("cleanup", "SELECT site, url, applied_at AS at FROM cleanup_ops WHERE status='applied'"),
                      ("prune", "SELECT site, url, applied_at AS at FROM prune_ops WHERE status='applied'")):
        try:
            rows += [(kind, r["site"], r["url"], r["at"]) for r in db.execute(sql)]
        except Exception:                       # a table this install does not have yet
            continue
    out, pages_ = {}, []
    cutoff = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days)).isoformat()
    last = {r[0]: r[1] for r in db.execute("SELECT site, MAX(day) FROM crawl_days GROUP BY site")}
    for kind, site, url, at in rows:
        if not at or at < cutoff or site not in last:
            continue
        day = at[:10]
        p = _path(url).rstrip("/") or "/"
        hit = db.execute("SELECT MIN(day) FROM crawl_hits WHERE site=? AND bot='google' AND day>? AND "
                         "(path=? OR path=?)", (site, day, p, p + "/")).fetchone()[0]
        x = out.setdefault(kind, {"n": 0, "refetched": 0, "pending": 0})
        if last[site] <= day:
            x["pending"] += 1                   # no complete day after the change has been read yet
            continue
        x["n"] += 1
        x["refetched"] += 1 if hit else 0
        pages_.append({"kind": kind, "site": site, "url": url, "changed": at, "refetched": hit})
    return {"kinds": out, "pages": sorted(pages_, key=lambda x: x["changed"], reverse=True)[:300]}


def raise_alerts(db, sites):
    """High alert when a site answered at least ERROR_SHARE of Googlebot's fetches on the last read day with 5xx/429."""
    from . import alerts
    for site in sites:
        last = db.execute("SELECT MAX(day) FROM crawl_days WHERE site=?", (site,)).fetchone()[0]
        if not last:
            continue
        r = db.execute("""SELECT SUM(hits) AS n, SUM(CASE WHEN status>=500 OR status=429 THEN hits ELSE 0 END) AS bad
                          FROM crawl_hits WHERE site=? AND day=? AND bot='google'""", (site, last)).fetchone()
        n, bad = r["n"] or 0, r["bad"] or 0
        week = (dt.date.fromisoformat(last) - dt.timedelta(days=6)).isoformat()
        w = db.execute("""SELECT SUM(hits) AS n, SUM(CASE WHEN status IN (404, 410) THEN hits ELSE 0 END) AS gone
                          FROM crawl_hits WHERE site=? AND day>=? AND bot='google'""", (site, week)).fetchone()
        wn, gone = w["n"] or 0, w["gone"] or 0
        if wn >= MISSING_MIN_HITS and gone / wn >= MISSING_SHARE:
            top = [x[0] for x in db.execute("""SELECT path FROM crawl_hits WHERE site=? AND day>=? AND bot='google'
                                               AND status IN (404, 410) GROUP BY path ORDER BY SUM(hits) DESC LIMIT 3""",
                                            (site, week))]
            alerts.add(db, site, None, "googlebot_missing", "warn",
                       f"{gone} of Googlebot's {wn} fetches in the 7 days to {last} ({gone / wn:.0%}) were pages that do not "
                       f"exist (404/410), for example {', '.join(top)}. Old URLs that still have links, or a broken pattern in "
                       "the site's links; a 301 to the right page, or removing the links, gives that crawl back to real pages.")
        if n >= ERROR_MIN_HITS and bad / n >= ERROR_SHARE:
            alerts.add(db, site, None, "googlebot_errors", "high",
                       f"Googlebot got a server error or 'too many requests' on {bad} of {n} fetches ({bad / n:.0%}) on "
                       f"{last} (UTC), from Cloudflare's analytics. Google slows its crawling of a site that does this, "
                       "and drops pages that keep failing.")
