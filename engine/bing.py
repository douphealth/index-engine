# -*- coding: utf-8 -*-
"""Bing Webmaster API: page traffic (to protect pages that earn Bing clicks), URL submission and crawl status.

Bing sends most of this portfolio's search clicks, so it gets first-class treatment: every
indexable URL is pushed once, then again only when it changes or Bing still has not crawled it.
"""
import datetime as dt
import hashlib
import re
import time

import requests

from . import config, store
from .util import days_since, log, norm, now_iso

BASE = "https://ssl.bing.com/webmaster/api.svc/json/"


class BingClient:
    def __init__(self, key):
        self.key = key

    def _get(self, method, **params):
        params["apikey"] = self.key
        r = requests.get(BASE + method, params=params, headers={"User-Agent": config.UA}, timeout=90)
        if r.status_code != 200:
            raise RuntimeError(f"{method} HTTP {r.status_code}: {r.text[:160]}")
        return r.json().get("d")

    def user_sites(self):
        return self._get("GetUserSites") or []

    def quota(self, site_url):
        d = self._get("GetUrlSubmissionQuota", siteUrl=site_url) or {}
        return int(d.get("DailyQuota") or 0), int(d.get("MonthlyQuota") or 0)

    def submit(self, site_url, urls):
        r = requests.post(BASE + "SubmitUrlBatch", params={"apikey": self.key}, json={"siteUrl": site_url, "urlList": urls},
                          headers={"User-Agent": config.UA}, timeout=120)
        return r.status_code, r.text[:200]

    def page_stats(self, site_url):
        return self._get("GetPageStats", siteUrl=site_url) or []

    def url_info(self, site_url, url):
        return self._get("GetUrlInfo", siteUrl=site_url, url=url) or {}

    def rank_traffic(self, site_url):
        """Daily clicks and impressions for the whole site (about 16 months)."""
        return self._get("GetRankAndTrafficStats", siteUrl=site_url) or []

    def crawl_stats(self, site_url):
        """Bing's daily crawl report: pages in its index, pages crawled, errors, robots blocks, 4xx/5xx."""
        return self._get("GetCrawlStats", siteUrl=site_url) or []


def working_client(keys):
    """First key Bing accepts (GetUserSites), as a client; None if none works.
    The key that worked last time is tried first (remembered by hash only, in out/)."""
    hint_file = config.OUT / "bing_key_hint.txt"
    try:
        hint = hint_file.read_text(encoding="utf-8").strip()
    except OSError:
        hint = ""
    digest = lambda k: hashlib.sha256(k.encode()).hexdigest()  # noqa: E731
    for key in sorted(keys, key=lambda k: digest(k) != hint):
        client = BingClient(key)
        try:
            client.user_sites()
            if digest(key) != hint:
                hint_file.parent.mkdir(parents=True, exist_ok=True)
                hint_file.write_text(digest(key), encoding="utf-8")
            return client
        except Exception as e:
            log(f"[bing] a key candidate was rejected: {str(e)[:80]}")
    return None


def bing_date(s):
    m = re.search(r"/Date\((-?\d+)", str(s or ""))
    if not m or int(m.group(1)) < 946_684_800_000:              # before 2000: Bing's year-1 "never" value
        return None
    return (dt.datetime(1970, 1, 1, tzinfo=dt.timezone.utc) + dt.timedelta(milliseconds=int(m.group(1)))).date().isoformat()


def resolve_sites(db, client, sites):
    try:
        entries = client.user_sites()
    except Exception as e:
        log(f"[bing] GetUserSites failed: {e}")
        return {}
    out = {}
    for site in sites:
        dom = site.split("://")[-1].rstrip("/")
        match = [e for e in entries if re.search(r"(^|//|\.)" + re.escape(dom) + r"(/|$)", e.get("Url", ""))]
        if match:
            out[site] = match[0]["Url"]
            store.set_meta(db, site, bing_site=match[0]["Url"], bing_verified=match[0].get("IsVerified"))
        else:
            store.set_meta(db, site, bing_site=None)
            log(f"[bing] {site}: not in the Bing Webmaster account")
    return out


def pull_performance(db, client, bsites):
    for site, burl in bsites.items():
        try:
            rows = client.page_stats(burl)
        except Exception as e:
            log(f"[bing] {site}: GetPageStats failed: {e}")
            continue
        agg, daily = {}, {}
        from . import traffic
        alias, seen = traffic.aliaser(db, site)
        for r in rows:
            k = alias(norm(r.get("Query") or ""))
            if k:
                c, i = agg.get(k, (0, 0))
                agg[k] = (c + int(r.get("Clicks") or 0), i + int(r.get("Impressions") or 0))
                day = bing_date(r.get("Date"))
                if day:
                    d = daily.setdefault(k, {})
                    cc, ii = d.get(day, (0, 0))
                    d[day] = (cc + int(r.get("Clicks") or 0), ii + int(r.get("Impressions") or 0))
        traffic.save_variants(db, site, seen)
        store_page_trend(db, site, daily)
        now = now_iso()
        db.execute("UPDATE perf SET bing_clicks=0, bing_impr=0 WHERE site=?", (site,))
        for url, (c, i) in agg.items():
            db.execute("""INSERT INTO perf(url, site, bing_clicks, bing_impr, bing_at) VALUES(?,?,?,?,?)
                          ON CONFLICT(url) DO UPDATE SET bing_clicks=excluded.bing_clicks, bing_impr=excluded.bing_impr,
                          bing_at=excluded.bing_at""", (url, site, c, i, now))
        db.commit()
        log(f"[bing] {site}: {len(agg)} pages with Bing impressions, {sum(1 for c, _ in agg.values() if c)} with clicks")


def store_page_trend(db, site, daily):
    """Per-page clicks/impressions in the latest 28 days of Bing's data vs the 28 before. The window ends on the last day
    Bing reported for the SITE (traffic_daily), so every page is measured over the same days."""
    from . import traffic
    end = traffic.series_end(db, site, "bing") or max((d for v in daily.values() for d in v), default=None)
    if not end:
        return
    (n0, n1), (p0, p1) = traffic.windows(end)
    db.execute("UPDATE page_trend SET b_now=0, b_prev=0, bi_now=0, bi_prev=0 WHERE site=?", (site,))
    now = now_iso()
    for url, days in daily.items():
        v = [0, 0, 0, 0]
        for day, (c, i) in days.items():
            if n0 <= day <= n1:
                v[0] += c; v[2] += i
            elif p0 <= day <= p1:
                v[1] += c; v[3] += i
        if any(v):
            db.execute("""INSERT INTO page_trend(url, site, b_now, b_prev, bi_now, bi_prev, b_end, at) VALUES(?,?,?,?,?,?,?,?)
                          ON CONFLICT(url) DO UPDATE SET b_now=excluded.b_now, b_prev=excluded.b_prev, bi_now=excluded.bi_now,
                          bi_prev=excluded.bi_prev, b_end=excluded.b_end, at=excluded.at""",
                       (url, site, v[0], v[1], v[2], v[3], end, now))


PAUSE_S = 0.4               # between GetUrlInfo calls: Bing throttles the whole account when they come too fast
MAX_TOTAL = 300             # per run, across all sites
SITE_FAIL_LIMIT = 3         # consecutive non-throttle errors before a site's queue is dropped for this run


def check_url_info(db, client, bsites, max_per_site=200, max_total=MAX_TOTAL):
    """Ask Bing whether it has crawled URLs we submitted at least 2 days ago and that it has never shown in
    results (a page with Bing impressions is plainly indexed, so checking it wastes a call). Oldest checks first.

    Bing's GetUrlInfo limit is per ACCOUNT (ThrottleUser / ThrottleHost), so the sites take turns, one URL each, and
    the first throttle ends the whole check: the rest waits for the next run instead of burning calls that all fail.
    A URL is only marked "checked" after Bing answered for it."""
    queues = {}
    for site, burl in bsites.items():
        found = db.execute("""SELECT u.url, b.checked_at, MIN(s.submitted_at) AS first_sub FROM urls u
                              JOIN submissions s ON s.url=u.url AND s.http_status BETWEEN 200 AND 299
                              LEFT JOIN bing_info b ON b.url=u.url LEFT JOIN perf p ON p.url=u.url
                              WHERE u.site=? AND b.last_crawled IS NULL AND COALESCE(p.bing_impr, 0) = 0
                              GROUP BY u.url""", (site,)).fetchall()
        due = [r for r in found if (days_since(r["first_sub"]) or 0) >= 2
               and (r["checked_at"] is None or (days_since(r["checked_at"]) or 0) >= 3)]
        due.sort(key=lambda r: (r["checked_at"] is not None, r["checked_at"] or ""))
        if due:
            queues[site] = (burl, [r["url"] for r in due[:max_per_site]])
    checked, crawled, fails, total, throttled = {}, {}, {}, 0, None
    while queues and total < max_total and not throttled:
        for site in list(queues):
            burl, urls = queues[site]
            if not urls:
                del queues[site]
                continue
            url = urls.pop(0)
            try:
                info = client.url_info(burl, url)
            except Exception as e:
                if "throttle" in str(e).lower():
                    throttled = str(e)[-70:]
                    break
                fails[site] = fails.get(site, 0) + 1
                log(f"[bing] {site}: GetUrlInfo failed for {url}: {str(e)[:100]}")
                if fails[site] >= SITE_FAIL_LIMIT:
                    log(f"[bing] {site}: GetUrlInfo keeps failing - skipping this site for now")
                    del queues[site]
                continue
            fails[site] = 0
            last = bing_date(info.get("LastCrawledDate"))
            checked[site] = checked.get(site, 0) + 1
            crawled[site] = crawled.get(site, 0) + bool(last)
            db.execute("INSERT OR REPLACE INTO bing_info VALUES(?,?,?,?,?)",
                       (url, now_iso(), bing_date(info.get("DiscoveryDate")), last, info.get("HttpStatus")))
            total += 1
            if total >= max_total:
                break
            if PAUSE_S:
                time.sleep(PAUSE_S)
    db.commit()
    for site, n in checked.items():
        left = len(queues.get(site, (None, []))[1])
        log(f"[bing] {site}: checked {n} URLs with GetUrlInfo, {crawled[site]} crawled by Bing" + (f", {left} left for the next run" if left else ""))
    if throttled:
        log(f"[bing] GetUrlInfo throttled after {total} checks ({throttled.strip()}): the rest waits for the next run")
    return total
