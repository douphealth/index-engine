# -*- coding: utf-8 -*-
"""Google: Search Console only. Pages with impressions, URL Inspection (quota-aware), sitemaps.

There is no API that makes Google index an article. The Indexing API is limited to JobPosting and
livestream (BroadcastEvent) pages, and Google says abuse can revoke access
(developers.google.com/search/apis/indexing-api/v3/quickstart, updated 16 Oct 2026), so this
module never calls it. What it does instead:
  * learn which URLs are already indexed for free (any Search impression in the last 14 days),
  * spend the 2,000/day URL Inspection quota per property only on URLs whose state is unknown or stale,
  * submit a sitemap only when it is unregistered or has gained URLs since the last submission.
"""
import datetime as dt
import threading
import time
from collections import defaultdict, deque
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from urllib.parse import quote

from . import alerts, config, store
from .util import days_since, log, norm, now_iso, pacific_day, parse_ts

API = "https://www.googleapis.com/webmasters/v3"
INSPECT_API = "https://searchconsole.googleapis.com/v1/urlInspection/index:inspect"


class QuotaExceeded(Exception):
    pass


class GSCClient:
    def __init__(self, sa):
        self.sa = sa
        self._tls = threading.local()

    def _s(self):
        s = getattr(self._tls, "s", None)
        if s is None:
            from google.auth.transport.requests import AuthorizedSession
            from google.oauth2 import service_account
            creds = service_account.Credentials.from_service_account_info(
                self.sa, scopes=["https://www.googleapis.com/auth/webmasters"])
            s = self._tls.s = AuthorizedSession(creds)
        return s

    def list_sites(self):
        r = self._s().get(API + "/sites", timeout=60)
        r.raise_for_status()
        return r.json().get("siteEntry", [])

    def search_pages(self, prop, start, end):
        url = f"{API}/sites/{quote(prop, safe='')}/searchAnalytics/query"
        rows, start_row = [], 0
        while True:
            body = {"startDate": str(start), "endDate": str(end), "dimensions": ["page"], "dataState": "all",
                    "rowLimit": 25000, "startRow": start_row}
            r = self._s().post(url, json=body, timeout=180)
            if r.status_code != 200:
                raise RuntimeError(f"searchAnalytics HTTP {r.status_code}: {r.text[:160]}")
            got = r.json().get("rows", [])
            rows.extend(got)
            if len(got) < 25000:
                return rows
            start_row += len(got)

    def inspect(self, prop, url):
        r = self._s().post(INSPECT_API, json={"inspectionUrl": url, "siteUrl": prop, "languageCode": "en-US"}, timeout=90)
        if r.status_code == 429 or (r.status_code == 403 and "quota" in r.text.lower()):
            raise QuotaExceeded(r.text[:160])
        if r.status_code != 200:
            return {"error": f"HTTP {r.status_code}: {r.text[:160]}", "http": r.status_code}
        res = r.json().get("inspectionResult", {}).get("indexStatusResult", {})
        return {"verdict": res.get("verdict"), "coverage": res.get("coverageState"),
                "indexing_state": res.get("indexingState"), "fetch_state": res.get("pageFetchState"),
                "robots_state": res.get("robotsTxtState"), "last_crawl": res.get("lastCrawlTime"),
                "google_canonical": res.get("googleCanonical"), "user_canonical": res.get("userCanonical")}

    def list_sitemaps(self, prop):
        r = self._s().get(f"{API}/sites/{quote(prop, safe='')}/sitemaps", timeout=60)
        r.raise_for_status()
        return r.json().get("sitemap", [])

    def submit_sitemap(self, prop, feed):
        r = self._s().put(f"{API}/sites/{quote(prop, safe='')}/sitemaps/{quote(feed, safe='')}", timeout=60)
        return r.status_code


def state_of(coverage, verdict=None):
    c = (coverage or "").lower()
    if not c and not verdict:
        return None
    if "unknown to google" in c:
        return "unknown"
    if "discovered" in c:
        return "discovered"
    if "crawled" in c and "not indexed" in c:
        return "crawled"
    if verdict == "PASS" or ("indexed" in c and "not indexed" not in c):
        return "indexed"
    return "other"


def resolve_properties(db, client, sites):
    try:
        entries = client.list_sites()
    except Exception as e:
        log(f"[google] sites.list failed: {e}")
        return {}
    perms = {e.get("siteUrl"): e.get("permissionLevel") for e in entries}
    out = {}
    for site in sites:
        dom = site.split("://")[-1].rstrip("/")
        for cand in (f"sc-domain:{dom}", f"https://{dom}/", f"https://www.{dom}/", f"http://{dom}/", f"http://www.{dom}/"):
            if cand in perms and perms[cand] != "siteUnverifiedUser":
                out[site] = cand
                store.set_meta(db, site, gsc_property=cand, gsc_permission=perms[cand])
                break
        else:
            store.set_meta(db, site, gsc_property=None)
            log(f"[google] {site}: the service account has no access to a Search Console property")
    return out


def pull_performance(db, client, props):
    end = dt.date.today() - dt.timedelta(days=2)
    for site, prop in props.items():
        try:
            rows90 = client.search_pages(prop, end - dt.timedelta(days=config.PERF_DAYS - 1), end)
            recent = client.search_pages(prop, end - dt.timedelta(days=config.EVIDENCE_DAYS - 1), end)
        except Exception as e:
            log(f"[google] {site}: performance pull failed: {e}")
            continue
        agg = defaultdict(lambda: [0, 0, 0])
        for r in rows90:
            k = norm(r["keys"][0])
            agg[k][0] += int(r.get("clicks", 0))
            agg[k][1] += int(r.get("impressions", 0))
        for r in recent:
            agg[norm(r["keys"][0])][2] += int(r.get("impressions", 0))
        now = now_iso()
        db.execute("UPDATE perf SET gsc_clicks=0, gsc_impr=0, gsc_recent_impr=0 WHERE site=?", (site,))
        for url, (c, i, ri) in agg.items():
            if not url:
                continue
            db.execute("""INSERT INTO perf(url, site, gsc_clicks, gsc_impr, gsc_recent_impr, gsc_at) VALUES(?,?,?,?,?,?)
                          ON CONFLICT(url) DO UPDATE SET gsc_clicks=excluded.gsc_clicks, gsc_impr=excluded.gsc_impr,
                          gsc_recent_impr=excluded.gsc_recent_impr, gsc_at=excluded.gsc_at""", (url, site, c, i, ri, now))
        db.commit()
        log(f"[google] {site}: {len(agg)} pages with impressions in {config.PERF_DAYS}d, "
            f"{sum(1 for v in agg.values() if v[2])} in the last {config.EVIDENCE_DAYS}d (counted as indexed, not inspected)")


def inspection_candidates(db, site, include_taxonomy=False):
    """URLs worth an inspection today, best first."""
    from .triage import value_score
    rows = db.execute("""
        SELECT u.url, u.kind, u.lastmod, a.status, a.noindex, a.canonical_state, a.hops, a.robots_google,
               g.inspected_at, g.coverage, g.verdict, g.error AS gerr,
               COALESCE(p.gsc_recent_impr,0) AS recent, COALESCE(p.gsc_clicks,0) AS gsc_clicks,
               COALESCE(p.gsc_impr,0) AS gsc_impr, COALESCE(p.bing_clicks,0) AS bing_clicks,
               COALESCE(p.bing_impr,0) AS bing_impr, a.words, o.requested_at,
               (SELECT MAX(applied_at) FROM link_ops l WHERE l.target=u.url AND l.status='applied') AS linked_at
        FROM urls u LEFT JOIN audits a ON a.url=u.url LEFT JOIN gsc g ON g.url=u.url LEFT JOIN perf p ON p.url=u.url
        LEFT JOIN owner_state o ON o.url=u.url
        WHERE u.site=? AND (u.in_sitemap=1 OR u.in_wp=1)""", (site,)).fetchall()
    out = []
    for r in rows:
        if r["kind"] == "taxonomy" and not include_taxonomy:
            continue
        if r["recent"] > 0:
            continue                                   # Search impressions prove it is indexed
        if r["status"] is not None and (r["status"] != 200 or r["noindex"] or r["hops"] or not r["robots_google"]
                                        or r["canonical_state"] == "other"):
            continue                                   # a technical blocker: fix first, inspecting tells us nothing new
        st = "error" if r["gerr"] else state_of(r["coverage"], r["verdict"])
        age = days_since(r["inspected_at"])
        if st is not None and age is not None and age < config.REINSPECT_DAYS.get(st, 10):
            seen = parse_ts(r["inspected_at"])
            changed = parse_ts(r["lastmod"]) and parse_ts(r["lastmod"]) > seen
            # Measure an action's effect a few days after it: a manual request, or new internal links.
            acted = any(parse_ts(t) and parse_ts(t) > seen and (days_since(t) or 0) >= d
                        for t, d in ((r["requested_at"], 2), (r["linked_at"], 3)))
            if not (changed or acted):
                continue                               # fresh enough, and nothing happened since
        never = st is None
        out.append((0 if never else 1, -value_score(r), r["url"]))
    out.sort()
    return [u for _, _, u in out]


def inspect_due(db, client, props, max_per_site=None, include_taxonomy=False, only_urls=None):
    """Inspect what is due per property (or exactly `only_urls`, e.g. pages published minutes ago), within quota."""
    day = pacific_day()
    queues, budget = {}, {}
    for site, prop in props.items():
        used, exhausted = store.quota_used(db, day, prop)
        left = 0 if exhausted else config.INSPECT_DAILY_LIMIT - config.INSPECT_SAFETY_MARGIN - used
        if max_per_site is not None:
            left = min(left, max_per_site)
        if only_urls is not None:
            mine = {r["url"] for r in db.execute("SELECT url FROM urls WHERE site=?", (site,))}
            cands = [u for u in only_urls if u in mine]
            if not cands:
                continue
        else:
            cands = inspection_candidates(db, site, include_taxonomy)
        budget[site] = max(0, left)
        queues[site] = deque(cands[:budget[site]])
        log(f"[google] {site}: {len(cands)} URLs need an inspection; quota left today {left}; inspecting {len(queues[site])}")
    total = sum(len(q) for q in queues.values())
    if not total:
        return

    pace = {p: [threading.Lock(), 0.0] for p in props.values()}

    def call(prop, url):
        lock, _ = pace[prop]
        with lock:                                     # <= ~9/s per property, under the 600/minute cap
            wait_s = pace[prop][1] - time.time()
            pace[prop][1] = max(time.time(), pace[prop][1]) + 0.11
        if wait_s > 0:
            time.sleep(wait_s)
        return client.inspect(prop, url)

    inflight, active, stopped = {}, defaultdict(int), set()
    done_n, t0 = 0, time.time()
    counts = defaultdict(lambda: defaultdict(int))
    with ThreadPoolExecutor(max_workers=config.INSPECT_WORKERS_PER_PROPERTY * len(queues)) as ex:
        while True:
            for site, q in queues.items():
                while q and site not in stopped and active[site] < config.INSPECT_WORKERS_PER_PROPERTY:
                    url = q.popleft()
                    inflight[ex.submit(call, props[site], url)] = (site, url)
                    active[site] += 1
            if not inflight:
                break
            finished, _ = wait(inflight, return_when=FIRST_COMPLETED)
            for f in finished:
                site, url = inflight.pop(f)
                active[site] -= 1
                prop = props[site]
                try:
                    res = f.result()
                except QuotaExceeded:
                    store.quota_add(db, day, prop, 0, exhausted=True)
                    if site not in stopped:
                        log(f"[google] {site}: inspection quota exhausted for today")
                    stopped.add(site)
                    continue
                except Exception as e:
                    res = {"error": f"{type(e).__name__}: {str(e)[:150]}"}
                store.quota_add(db, day, prop, 1)
                if res.get("http") in (401, 403):
                    log(f"[google] {site}: {res['error']} - stopping this property")
                    stopped.add(site)
                now = now_iso()
                prev = db.execute("SELECT coverage, verdict FROM gsc WHERE url=?", (url,)).fetchone()
                if prev and not res.get("error"):
                    was, is_ = state_of(prev["coverage"], prev["verdict"]), state_of(res.get("coverage"), res.get("verdict"))
                    if was == "indexed" and is_ in ("crawled", "discovered", "unknown"):
                        alerts.add(db, site, url, "dropped_from_google", "high",
                                   f"Google no longer indexes this page: {res.get('coverage')}.")
                    elif was in ("crawled", "discovered", "unknown") and is_ == "indexed":
                        alerts.add(db, site, url, "newly_indexed", "good", f"Google indexed this page (was: {prev['coverage']}).")
                db.execute("""INSERT OR REPLACE INTO gsc(url, property, inspected_at, verdict, coverage, indexing_state,
                              fetch_state, robots_state, last_crawl, google_canonical, user_canonical, error)
                              VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                           (url, prop, now, res.get("verdict"), res.get("coverage"), res.get("indexing_state"),
                            res.get("fetch_state"), res.get("robots_state"), res.get("last_crawl"),
                            res.get("google_canonical"), res.get("user_canonical"), res.get("error")))
                db.execute("INSERT INTO gsc_history(url, inspected_at, coverage) VALUES(?,?,?)",
                           (url, now, res.get("coverage") or res.get("error")))
                counts[site][res.get("coverage") or "error"] += 1
                done_n += 1
                if done_n % 100 == 0 or done_n == total:
                    db.commit()
                    log(f"  inspected {done_n}/{total} ({done_n / max(1e-6, time.time() - t0):.1f}/s)")
    db.commit()
    for site, c in counts.items():
        log(f"[google] {site}: " + ", ".join(f"{v}x {k}" for k, v in sorted(c.items(), key=lambda kv: -kv[1])))


def sync_sitemaps(db, client, props, dry_run=False):
    for site, prop in props.items():
        meta = store.get_meta(db, site)
        try:
            registered = client.list_sitemaps(prop)
        except Exception as e:
            log(f"[google] {site}: sitemaps.list failed: {e}")
            continue
        status = []
        for sm in registered:
            status.append({"path": sm.get("path"), "errors": int(sm.get("errors") or 0), "warnings": int(sm.get("warnings") or 0),
                           "lastDownloaded": sm.get("lastDownloaded"), "isPending": sm.get("isPending")})
        store.set_meta(db, site, gsc_sitemaps=status)
        reg_paths = {s["path"] for s in status}
        n_urls = db.execute("SELECT COUNT(*) FROM urls WHERE site=? AND in_sitemap=1", (site,)).fetchone()[0]
        for feed in meta.get("sitemap_roots") or []:
            last = db.execute("SELECT url_count, submitted_at FROM sitemap_submits WHERE site=? AND feed=? AND http_status BETWEEN 200 AND 299 "
                              "ORDER BY submitted_at DESC, rowid DESC LIMIT 1", (site, feed)).fetchone()
            if last and n_urls <= last["url_count"] and (feed in reg_paths or (days_since(last["submitted_at"]) or 0) < 7):
                continue                               # nothing new (or just submitted and not listed yet)
            if feed in reg_paths and not last:
                # Registered before this app existed: record a baseline and resubmit only when URLs are added.
                db.execute("INSERT INTO sitemap_submits VALUES(?,?,?,?,?)", (site, feed, now_iso(), n_urls, 200))
                continue
            why = "not registered in Search Console" if feed not in reg_paths else f"{n_urls - last['url_count']} new URLs"
            if dry_run:
                log(f"[google] {site}: would submit {feed} ({why})")
                continue
            code = client.submit_sitemap(prop, feed)
            db.execute("INSERT INTO sitemap_submits VALUES(?,?,?,?,?)", (site, feed, now_iso(), n_urls, code))
            log(f"[google] {site}: submitted {feed} ({why}) -> HTTP {code}"
                + (" (the service account needs Owner or Full permission to submit sitemaps)" if code == 403 else ""))
        db.commit()
