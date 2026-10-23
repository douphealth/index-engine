# -*- coding: utf-8 -*-
"""Bing Webmaster API: page traffic (to protect pages that earn Bing clicks), URL submission and crawl status.

Bing sends most of this portfolio's search clicks, so it gets first-class treatment: every
indexable URL is pushed once, then again only when it changes or Bing still has not crawled it.
"""
import datetime as dt
import hashlib
import re

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
        agg = {}
        for r in rows:
            k = norm(r.get("Query") or "")
            if k:
                c, i = agg.get(k, (0, 0))
                agg[k] = (c + int(r.get("Clicks") or 0), i + int(r.get("Impressions") or 0))
        now = now_iso()
        db.execute("UPDATE perf SET bing_clicks=0, bing_impr=0 WHERE site=?", (site,))
        for url, (c, i) in agg.items():
            db.execute("""INSERT INTO perf(url, site, bing_clicks, bing_impr, bing_at) VALUES(?,?,?,?,?)
                          ON CONFLICT(url) DO UPDATE SET bing_clicks=excluded.bing_clicks, bing_impr=excluded.bing_impr,
                          bing_at=excluded.bing_at""", (url, site, c, i, now))
        db.commit()
        log(f"[bing] {site}: {len(agg)} pages with Bing impressions, {sum(1 for c, _ in agg.values() if c)} with clicks")


def check_url_info(db, client, bsites, max_per_site=200):
    """Ask Bing whether it has crawled URLs we submitted at least 2 days ago and that it has never shown in
    results (a page with Bing impressions is plainly indexed, so checking it wastes a call). Oldest checks first."""
    for site, burl in bsites.items():
        found = db.execute("""SELECT u.url, b.checked_at, MIN(s.submitted_at) AS first_sub FROM urls u
                              JOIN submissions s ON s.url=u.url AND s.http_status BETWEEN 200 AND 299
                              LEFT JOIN bing_info b ON b.url=u.url LEFT JOIN perf p ON p.url=u.url
                              WHERE u.site=? AND b.last_crawled IS NULL AND COALESCE(p.bing_impr, 0) = 0
                              GROUP BY u.url""", (site,)).fetchall()
        due = [r for r in found if (days_since(r["first_sub"]) or 0) >= 2
               and (r["checked_at"] is None or (days_since(r["checked_at"]) or 0) >= 3)]
        due.sort(key=lambda r: (r["checked_at"] is not None, r["checked_at"] or ""))
        rows = [r["url"] for r in due[:max_per_site]]
        crawled = 0
        for url in rows:
            try:
                info = client.url_info(burl, url)
            except Exception as e:
                log(f"[bing] {site}: GetUrlInfo stopped: {e}")
                break
            last = bing_date(info.get("LastCrawledDate"))
            crawled += bool(last)
            db.execute("INSERT OR REPLACE INTO bing_info VALUES(?,?,?,?,?)",
                       (url, now_iso(), bing_date(info.get("DiscoveryDate")), last, info.get("HttpStatus")))
        db.commit()
        if rows:
            log(f"[bing] {site}: checked {len(rows)} URLs with GetUrlInfo, {crawled} crawled by Bing")
