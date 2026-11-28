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

Only real crawlers count (since 28 Nov 2026). Anyone can send a Googlebot user agent: on gearuptofit.com in the week to
27 Nov 2026, 2,366 of 5,570 "Googlebot" requests came from the owner's own PC (site_guard.py checks pages as Googlebot)
and 509 from other networks; only 2,695 were Google. Each request's IP is checked against the ranges Google and Bing
publish for their crawlers (VERIFY_LISTS, cached for a week); the rest are counted per day as impostors and nothing
else. Without a range list (never downloaded) nothing is read, rather than counting unverified requests.

Limits, stated where the numbers are shown: Cloudflare samples busy zones ("adaptive"), so counts are estimates. Days
are UTC. A site whose DNS is not on a Cloudflare account the token can read has no telemetry.
"""
import ipaddress
import json
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
  verified INT DEFAULT 0, fake_google INT DEFAULT 0, fake_bing INT DEFAULT 0, PRIMARY KEY(site, day));
CREATE INDEX IF NOT EXISTS crawl_hits_path ON crawl_hits(site, path);
"""

SPAN_DAYS = 7                   # days read per query (one call reads both crawlers); a full span is split in half
Q = """query($zone: String!, $since: Time!, $until: Time!, $host: String!) { viewer { zones(filter: {zoneTag: $zone}) {
  %s } } }""" % " ".join(
    f"""{bot}: httpRequestsAdaptiveGroups(limit: {ROW_LIMIT}, filter: {{datetime_geq: $since, datetime_lt: $until,
    clientRequestHTTPHost: $host, userAgent_like: "{ua}"}}) {{ count dimensions {{ date clientRequestPath edgeResponseStatus
    clientIP }} }}"""
    for bot, ua in BOTS.items())

# The address ranges each search engine publishes for its own crawlers: the documented way to verify one.
VERIFY_LISTS = {
    "google": ["https://developers.google.com/static/search/apis/ipranges/googlebot.json",
               "https://developers.google.com/static/search/apis/ipranges/special-crawlers.json"],
    "bing": ["https://www.bing.com/toolbox/bingbot.json"],
}
RANGES_FILE = config.OUT / "crawler_ranges.json"
RANGES_MAX_AGE_DAYS = 7


def _migrate(db):
    """Columns added to crawl_days on 28 Nov 2026 (verified crawlers); days read before them are read again."""
    db.executescript(SCHEMA)
    have = {r[1] for r in db.execute("PRAGMA table_info(crawl_days)")}
    if "verified" not in have:                      # everything stored so far counted impostors too: read it again
        for col in ("verified", "fake_google", "fake_bing"):
            if col not in have:
                db.execute(f"ALTER TABLE crawl_days ADD COLUMN {col} INT DEFAULT 0")
        db.execute("DELETE FROM crawl_hits")
        db.execute("DELETE FROM crawl_days")


def _parse_ranges(data):
    out = []
    for p in (data or {}).get("prefixes") or []:
        net = p.get("ipv4Prefix") or p.get("ipv6Prefix")
        try:
            out.append(ipaddress.ip_network(net, strict=False))
        except (TypeError, ValueError):
            continue
    return out


def ranges(session=None, path=None, now=None):
    """{"google": [networks], "bing": [networks]} from the engines' published lists, kept in RANGES_FILE and downloaded
    again after RANGES_MAX_AGE_DAYS. A failed download keeps the last good copy; None when there has never been one."""
    path = path or RANGES_FILE
    now = now or dt.datetime.now(dt.timezone.utc)
    try:
        cached = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        cached = None
    fresh = cached and (now - dt.datetime.fromisoformat(cached["at"])).days < RANGES_MAX_AGE_DAYS
    if not fresh:
        s = session or requests
        got = {}
        try:
            for bot, urls in VERIFY_LISTS.items():
                nets = []
                for u in urls:
                    r = s.get(u, timeout=30)
                    r.raise_for_status()
                    nets += [str(n) for n in _parse_ranges(r.json())]
                if not nets:
                    raise ValueError(f"no {bot} ranges in the published list")
                got[bot] = list(dict.fromkeys(nets))
            cached = {"at": now.isoformat(), "nets": got}
            Path(path).parent.mkdir(parents=True, exist_ok=True)
            Path(path).write_text(json.dumps(cached), encoding="utf-8")
        except (requests.RequestException, ValueError, KeyError) as e:
            log(f"[crawl] the crawlers' published address lists could not be downloaded ({str(e)[:100]})"
                + ("; using the copy from " + cached["at"][:10] if cached else ""))
    if not cached:
        return None
    return {bot: [ipaddress.ip_network(n) for n in nets] for bot, nets in cached["nets"].items()}


class Verifier:
    """is_real(bot, ip): whether an address is in that engine's published crawler ranges. Remembers each answer."""
    def __init__(self, nets):
        self.nets = {bot: {4: [n for n in v if n.version == 4], 6: [n for n in v if n.version == 6]} for bot, v in nets.items()}
        self.seen = {}

    def is_real(self, bot, ip):
        key = (bot, ip)
        if key not in self.seen:
            try:
                a = ipaddress.ip_address(ip)
                self.seen[key] = any(a in n for n in self.nets.get(bot, {}).get(a.version, []))
            except ValueError:
                self.seen[key] = False
        return self.seen[key]


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
    """Complete UTC days not read yet with verified crawlers, oldest first (at most `keep` days back)."""
    today = today or dt.datetime.now(dt.timezone.utc).date()
    have = {r[0] for r in db.execute("SELECT day FROM crawl_days WHERE site=? AND verified=1", (site,))}
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


def read_span(token, zone, host, days, session=None, verifier=None):
    """({day: {(bot, path, status): hits}}, {day: {bot: impostor hits}}, truncated) for one site over consecutive UTC
    days (every day present, even with no hits). Only requests from the engine's own published ranges count; the rest
    are impostors. A span that fills the row limit is read again in halves."""
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
        a, fa, ta = read_span(token, zone, host, days[:half], session, verifier)
        b, fb, tb = read_span(token, zone, host, days[half:], session, verifier)
        return {**a, **b}, {**fa, **fb}, ta or tb
    out, fake = {d: {} for d in days}, {d: {} for d in days}
    for bot in BOTS:
        for g in got.get(bot) or []:
            dim = g["dimensions"]
            if verifier is not None and not verifier.is_real(bot, dim.get("clientIP") or ""):
                f = fake.setdefault(dim["date"], {})
                f[bot] = f.get(bot, 0) + int(g["count"])
                continue
            day = out.setdefault(dim["date"], {})
            key = (bot, dim["clientRequestPath"] or "/", int(dim["edgeResponseStatus"] or 0))
            day[key] = day.get(key, 0) + int(g["count"])
    return out, fake, full


def pull(db, sites, session=None, today=None, max_days=KEEP_DAYS):
    """Read the complete days not read yet for every site with a readable zone. Read-only towards Cloudflare.
    Returns {site: days read}. Several sites are read at once; the database is written in order (util.fetched)."""
    _migrate(db)
    toks = tokens()
    if not toks:
        return {}
    nets = ranges(session)
    if not nets:
        log("[crawl] no published crawler address list yet, so nothing is read: unverified counts would include impostors")
        return {}
    verifier = Verifier(nets)
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
            by_day, fake, truncated = read_span(tok, zid, _host(site), span, session, verifier)
            got += [(day, by_day.get(day) or {}, fake.get(day) or {}, truncated) for day in span]
        return got

    out = {}
    for (site, _z, days), got, err in fetched(todo, read):
        if err:
            log(f"[crawl] {site}: Cloudflare analytics could not be read ({str(err)[:140]})")
            continue
        for day, hits, fake, truncated in got:
            db.execute("DELETE FROM crawl_hits WHERE site=? AND day=?", (site, day))
            db.executemany("INSERT INTO crawl_hits VALUES(?,?,?,?,?,?)",
                           [(site, day, bot, path, status, n) for (bot, path, status), n in hits.items()])
            db.execute("INSERT OR REPLACE INTO crawl_days(site, day, read_at, rows, truncated, verified, fake_google, fake_bing) "
                       "VALUES(?,?,?,?,?,1,?,?)", (site, day, now_iso(), len(hits), int(truncated),
                                                   fake.get("google", 0), fake.get("bing", 0)))
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
    _migrate(db)
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


MISSING_KINDS = (("plugin or theme file", lambda p: p.startswith("/wp-content/plugins/") or p.startswith("/wp-content/themes/")),
                 ("image or file", lambda p: p.startswith("/wp-content/uploads/") or "." in p.rsplit("/", 1)[-1]),
                 ("archive page past the end", lambda p: "/page/" in p),
                 ("page", lambda p: True))


def missing_kind(path):
    """What kind of address a missing path is. Pure (tested)."""
    return next(name for name, test in MISSING_KINDS if test(path))


def moved_to(db, site, path):
    """A live page of the site with the same last address segment, when the missing path is a page: the post most
    likely moved (a category added to its address) without a redirect."""
    seg = [x for x in path.split("/") if x]
    if not seg or missing_kind(path) != "page":
        return None
    hits = [r[0] for r in db.execute("SELECT url FROM urls WHERE site=? AND (url LIKE ? OR url LIKE ?)",
                                     (site, f"%/{seg[-1]}/", f"%/{seg[-1]}"))]
    hits = [u for u in hits if (urlsplit(u).path.rstrip("/") or "/") != (path.rstrip("/") or "/")]
    return hits[0] if len(hits) == 1 else None


def site_summary(db, site):
    """Per-day crawler totals and what the site answered Googlebot, for the dashboard. None without telemetry."""
    _migrate(db)
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
    def blocked(url):                                    # the new address is noindex: a 301 gains nothing yet
        try:
            r = db.execute("SELECT noindex FROM audits WHERE url=?", (url,)).fetchone()
        except Exception:                                # a database without audits (tests)
            return None
        return bool(r and r[0]) if r else None

    errors = [{"path": r["path"], "status": r["status"], "hits": r["n"], "kind": missing_kind(r["path"]),
               "moved_to": (m := moved_to(db, site, r["path"]) if r["status"] in (404, 410) else None),
               "moved_noindex": blocked(m) if m else None} for r in db.execute(
        """SELECT path, status, SUM(hits) AS n FROM crawl_hits WHERE site=? AND bot='google' AND day>=? AND status>=400
           GROUP BY path, status ORDER BY n DESC LIMIT 15""", (site, week))]
    missing = defaultdict(lambda: [0, 0])                 # kind -> [fetches, addresses]
    for r in db.execute("""SELECT path, SUM(hits) AS n FROM crawl_hits WHERE site=? AND bot='google' AND day>=?
                           AND status IN (404, 410) GROUP BY path""", (site, week)):
        m = missing[missing_kind(r["path"])]
        m[0] += r["n"]
        m[1] += 1
    fake = db.execute("SELECT SUM(fake_google), SUM(fake_bing) FROM crawl_days WHERE site=? AND day>=?", (site, week)).fetchone()
    return {"days": len(days), "last_day": days[-1], "series": [[d, *series[d]] for d in days],
            "googlebot_7d": g7, "bingbot_7d": sum(series[d][2] for d in days if d >= week),
            "status_7d": dict(status), "errors_7d": errors,
            "missing_7d": {k: {"hits": v[0], "paths": v[1]} for k, v in sorted(missing.items(), key=lambda kv: -kv[1][0])},
            "impostors_7d": {"google": fake[0] or 0, "bing": fake[1] or 0},
            "pages_fetched_30d": sum(1 for x in known.values() if x["g_30d"]), "pages_known": n_urls,
            "truncated": bool(db.execute("SELECT 1 FROM crawl_days WHERE site=? AND truncated=1", (site,)).fetchone())}


def after_changes(db, days=30):
    """For the engine's own changes of the last `days` days (applied links, cleanups, noindex/redirects): did Googlebot
    fetch the changed page again after the change? {kind: {"n", "refetched", "pending"}} plus the pages."""
    _migrate(db)
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
                       f"{gone} of Googlebot's {wn} verified fetches in the 7 days to {last} ({gone / wn:.0%}) were addresses "
                       f"that do not exist (404/410), for example {', '.join(top)}. Google keeps re-checking addresses it once "
                       "knew, which is normal and does not hurt rankings; the Crawlers page sorts them by kind and names a "
                       "live page where one only moved, which is the part worth a 301.")
        if n >= ERROR_MIN_HITS and bad / n >= ERROR_SHARE:
            alerts.add(db, site, None, "googlebot_errors", "high",
                       f"Googlebot got a server error or 'too many requests' on {bad} of {n} fetches ({bad / n:.0%}) on "
                       f"{last} (UTC), from Cloudflare's analytics. Google slows its crawling of a site that does this, "
                       "and drops pages that keep failing.")
