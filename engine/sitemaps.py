# -*- coding: utf-8 -*-
"""Tell the search engines when a site's URL list changes, through the channels they document.

  * Every sitemap root and the WordPress RSS feed are registered with Google (Search Console API) and Bing
    (Webmaster API SubmitFeed). Google accepts an RSS 2.0 or Atom 1.0 feed as a sitemap. The XML sitemap is the
    complete inventory; the feed carries recent changes.
  * A registered sitemap is resubmitted when URLs were ADDED or UPDATED since its last submission. The earlier rule
    (the URL count rose) missed a site that published 3 posts and removed 3, and every update to an existing post.
  * On the 10-minute fresh-content tick, a new or updated post triggers a resubmission only once the sitemap or feed
    the engines will read actually lists it. A cached sitemap is not resubmitted with old content, which would waste
    the submission; the daily run picks the post up instead.
  * When the feed declares a WebSub hub (<atom:link rel="hub">), the hub is told the feed changed. Nothing is pinged
    when the feed declares no hub: a publish notice to a hub nobody subscribes through reaches nobody.
  * lastmod is judged per site. Google says it uses lastmod only when it is consistently and verifiably accurate. A
    sitemap where every URL carries the same lastmod (the time the file was generated) or dates in the future is
    not trusted, here or by the engines, and the site gets a plain-English fix.

Nothing here forces indexing. These signals only make sure each engine knows about every URL and every change
quickly, and does not waste a crawl on a stale list.
"""
import datetime as dt
import xml.etree.ElementTree as ET
from collections import Counter

from . import config, store
from .util import days_since, get, http, log, norm, now_iso, parse_ts, same_site

ATOM = "{http://www.w3.org/2005/Atom}"
BASELINE = object()            # registered before this app recorded a submission: record, do not resubmit


# ---------------------------------------------------------------- feed

def detect_feed(base, site_host):
    """Fetch the WordPress feed. Returns {url, ok, items, links, hubs, why}; `links` is not stored in site meta."""
    url = base.rstrip("/") + "/feed/"
    try:
        r = get(url, timeout=(10, 30), headers={"Accept": "application/rss+xml, application/atom+xml, application/xml;q=0.9, */*;q=0.5"})
    except Exception as e:
        return {"url": url, "ok": False, "why": type(e).__name__}
    if r.status_code != 200:
        return {"url": url, "ok": False, "why": f"HTTP {r.status_code}"}
    final = norm(r.url) or url
    if not same_site(final, site_host):
        return {"url": url, "ok": False, "why": "redirects to another host"}
    try:
        root = ET.fromstring(r.content)
    except ET.ParseError:
        return {"url": final, "ok": False, "why": "not valid XML"}
    links, hubs = [], []
    if root.tag == "rss":
        links = [(it.findtext("link") or "").strip() for it in root.iter("item")]
    elif root.tag == ATOM + "feed":
        for entry in root.iter(ATOM + "entry"):
            for ln in entry.findall(ATOM + "link"):
                if ln.get("rel", "alternate") == "alternate" and ln.get("href"):
                    links.append(ln.get("href").strip())
                    break
    else:
        return {"url": final, "ok": False, "why": f"not an RSS or Atom feed ({root.tag[-20:]})"}
    for ln in root.iter(ATOM + "link"):
        href = (ln.get("href") or "").strip()
        if ln.get("rel") == "hub" and href.startswith(("https://", "http://")) and href not in hubs:
            hubs.append(href)
    links = [norm(u) for u in links if u and same_site(u, site_host)]
    ok = bool(links)
    return {"url": final, "ok": ok, "items": len(links), "links": links, "hubs": hubs[:3],
            "why": None if ok else "lists no posts of this site"}


def feed_meta(feed):
    """What is worth keeping in site meta (no per-item links)."""
    return {k: feed.get(k) for k in ("url", "ok", "items", "hubs", "why")} if feed else None


def feeds_for(meta):
    """Sitemap roots plus the RSS feed when it works: what gets registered with Google and Bing."""
    out = list(meta.get("sitemap_roots") or [])
    feed = meta.get("feed") or {}
    if feed.get("ok") and feed.get("url") and feed["url"] not in out:
        out.append(feed["url"])
    return out


# ---------------------------------------------------------------- lastmod

def lastmod_health(found, wp=None, now=None):
    """How far a site's sitemap lastmod values can be trusted.

    found: {url: (kind, sitemap, lastmod)} from discover.walk_sitemaps; wp: {url: (kind, modified_gmt, date_gmt)}."""
    now = now or dt.datetime.now(dt.timezone.utc)
    values = [lm for _k, _s, lm in found.values() if lm]
    n = len(found)
    out = {"checked": n, "missing": n - len(values), "uniform": 0.0, "uniform_value": None, "future": 0,
           "compared": 0, "stale": 0}
    if values:
        value, count = Counter(values).most_common(1)[0]
        out["uniform"], out["uniform_value"] = round(count / len(values), 3), value
        out["future"] = sum(1 for v in values if (parse_ts(v) or now) > now + dt.timedelta(days=1))
    for url, (_kind, modified, _published) in (wp or {}).items():
        sm = found.get(url)
        wp_mod = parse_ts(modified)
        if not sm or not wp_mod:
            continue
        out["compared"] += 1
        sm_mod = parse_ts(sm[2])
        if sm_mod is not None and wp_mod - sm_mod > dt.timedelta(days=1):
            out["stale"] += 1
    return out


def uniform(h):
    return bool(h) and (h.get("checked") or 0) - (h.get("missing") or 0) >= 20 and (h.get("uniform") or 0) >= 0.8


def lastmod_trusted(meta):
    """False when the site's lastmod values carry no information about change (a generation stamp, or the future)."""
    h = meta.get("lastmod_health") or {}
    with_lm = (h.get("checked") or 0) - (h.get("missing") or 0)
    return not (uniform(h) or (with_lm and (h.get("future") or 0) / with_lm > 0.1))


def lastmod_messages(meta):
    """Plain-English findings for the site diagnosis: (kind, message)."""
    h = meta.get("lastmod_health") or {}
    n = h.get("checked") or 0
    msgs = []
    if n < 20:
        return msgs
    if uniform(h):
        msgs.append(("discovery", f"{h['uniform']:.0%} of the {n} sitemap URLs carry the same lastmod "
                                  f"({str(h.get('uniform_value'))[:16]}): that is when the sitemap was generated, not when each "
                                  "page changed. Google uses lastmod only when it is consistently accurate, so updates to existing "
                                  "pages are not being signalled. Set the sitemap plugin to use each post's modified date."))
    elif h.get("missing", 0) >= 0.5 * n:
        msgs.append(("discovery", f"{h['missing']} of {n} sitemap URLs have no lastmod, so search engines cannot tell which "
                                  "pages changed. Turn lastmod on in the sitemap plugin."))
    if h.get("future"):
        msgs.append(("fix", f"{h['future']} sitemap URLs carry a lastmod in the future. Engines stop trusting a sitemap's "
                            "lastmod when it is wrong: check the site's timezone setting and the sitemap plugin."))
    if h.get("stale", 0) >= 5 and h["stale"] >= 0.1 * max(1, h.get("compared") or 0):
        msgs.append(("discovery", f"{h['stale']} posts were updated in WordPress more than a day after the lastmod their sitemap "
                                  "entry shows. The sitemap is likely served from a cache (plugin, Worker or CDN), so engines "
                                  "hear about those updates late. Purge the sitemap cache, or shorten its lifetime."))
    return msgs


def feed_messages(meta):
    if not meta.get("wp"):
        return []
    feed = meta.get("feed")
    if not feed:
        return []
    if not feed.get("ok"):
        return [("info", f"The RSS feed at {feed.get('url')} does not work ({feed.get('why')}). Google accepts a feed as a "
                         "sitemap for recent changes; once it works, the engine registers it with Google and Bing.")]
    if not feed.get("hubs"):
        return [("info", "The RSS feed declares no WebSub hub, so new posts cannot be pushed to subscribers of the feed. "
                         "A WebSub plugin adds the hub link; the engine then notifies the hub on every new post.")]
    return []


def bing_messages(meta):
    msgs = []
    for sm in meta.get("bing_sitemaps") or []:
        age = days_since(sm.get("lastCrawled"))
        if age is not None and age > 30:
            msgs.append(("discovery", f"Bing last read {sm.get('path')} {age:.0f} days ago."))
    return msgs


# ---------------------------------------------------------------- when to (re)submit

def _row(db, site, feed, engine, ok_only):
    return db.execute(
        "SELECT url_count, submitted_at, http_status FROM sitemap_submits WHERE site=? AND feed=? "
        "AND COALESCE(engine, 'google')=?" + (" AND http_status BETWEEN 200 AND 299" if ok_only else "")
        + " ORDER BY submitted_at DESC, rowid DESC LIMIT 1", (site, feed, engine)).fetchone()


def changes_since(db, site, since, use_lastmod=True):
    """URLs of the site first seen, or (when lastmod is trusted) modified, after `since`."""
    since_t = parse_ts(since)
    if since_t is None:
        return 0
    limit = dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=1)       # a lastmod in the future says nothing
    n = 0
    for r in db.execute("SELECT first_seen, lastmod FROM urls WHERE site=? AND (in_sitemap=1 OR in_wp=1)", (site,)):
        seen = parse_ts(r["first_seen"])
        mod = parse_ts(r["lastmod"]) if use_lastmod else None
        if (seen and seen > since_t) or (mod and since_t < mod <= limit):
            n += 1
    return n


def decide(db, site, feed, engine, registered, n_urls, trust_lastmod=True, fresh=False):
    """Why to submit `feed` to `engine` now: a reason string, BASELINE, or None (nothing to do)."""
    attempt = _row(db, site, feed, engine, ok_only=False)
    if attempt and not 200 <= (attempt["http_status"] or 0) <= 299:
        code, age = attempt["http_status"] or 0, days_since(attempt["submitted_at"]) or 0
        wait_days = config.SITEMAP_DENIED_RETRY_DAYS if code in (401, 403) else 1 if 400 <= code < 500 else 1 / 24
        if age < wait_days:
            return None                                # the last attempt failed: do not hammer every tick
    last = _row(db, site, feed, engine, ok_only=True)
    if not registered:
        if last and (days_since(last["submitted_at"]) or 0) < 7:
            return None                                # just submitted; the engine has not listed it yet
        return "not registered"
    if not last:
        return BASELINE
    if (days_since(last["submitted_at"]) or 0) * 24 < config.SITEMAP_RESUBMIT_MIN_HOURS:
        return None
    if fresh:
        return "a new or updated post is now listed"
    n = changes_since(db, site, last["submitted_at"], use_lastmod=trust_lastmod)
    if n:
        return f"{n} new or updated URLs since the last submission"
    if n_urls > (last["url_count"] or 0):
        return f"{n_urls - last['url_count']} more URLs listed"
    return None


def record(db, site, feed, engine, n_urls, code):
    db.execute("INSERT INTO sitemap_submits(site, feed, submitted_at, url_count, http_status, engine) VALUES(?,?,?,?,?,?)",
               (site, feed, now_iso(), n_urls, code, engine))
    db.commit()


def listed_count(db, site):
    return db.execute("SELECT COUNT(*) FROM urls WHERE site=? AND in_sitemap=1", (site,)).fetchone()[0]


def submit_all(db, engine, site, registered, submit_fn, dry_run=False, only=None, accepts=None):
    """Register / resubmit every feed of `site` with one engine. submit_fn(feed) -> HTTP status; accepts(feed) -> bool
    leaves out feeds the engine cannot take for this site. Returns the number of accepted submissions."""
    meta = store.get_meta(db, site)
    n_urls = listed_count(db, site)
    trust = lastmod_trusted(meta)
    sent = 0
    for feed in feeds_for(meta):
        if (only is not None and feed not in only) or (accepts and not accepts(feed)):
            continue
        why = decide(db, site, feed, engine, feed in registered, n_urls, trust, fresh=only is not None)
        if why is None:
            continue
        if why is BASELINE:
            record(db, site, feed, engine, n_urls, 200)
            continue
        if dry_run:
            log(f"[{engine}] {site}: would submit {feed} ({why})")
            continue
        try:
            code = submit_fn(feed)
        except Exception as e:
            log(f"[{engine}] {site}: submitting {feed} failed ({type(e).__name__}: {str(e)[:100]})")
            code = 0
        record(db, site, feed, engine, n_urls, code)
        ok = 200 <= code <= 299
        sent += ok
        hint = ""
        if code == 403 and engine == "google":
            hint = " (the service account needs Owner or Full permission to submit sitemaps)"
        log(f"[{engine}] {site}: submitted {feed} ({why}) -> HTTP {code}{hint}")
    return sent


# ---------------------------------------------------------------- fresh content: is it listed yet?

def fresh_ready(db, changes):
    """{site: set(feeds)} whose content already lists at least one post from `changes` (at its new version).

    changes: {site: [(url, kind, modified, published), ...]} from fresh.changed()."""
    from .discover import walk_sitemaps
    out = {}
    for site, items in changes.items():
        meta = store.get_meta(db, site)
        base = meta.get("base") or config.site_base(site)
        host = base.split("://")[-1].split("/")[0]
        wanted = {url: parse_ts(modified) for url, _k, modified, _p in items}
        ready = set()
        roots = list(meta.get("sitemap_roots") or [])
        if roots:
            try:
                found, _read, _errors = walk_sitemaps(base, roots, host)
            except Exception as e:
                found = {}
                log(f"[sitemaps] {site}: could not re-read the sitemap ({type(e).__name__})")
            for url, mod in wanted.items():
                entry = found.get(url)
                if not entry:
                    continue
                lm = parse_ts(entry[2])
                if lm is None or mod is None or lm >= mod - dt.timedelta(minutes=2):
                    ready.update(roots)
                    db.execute("UPDATE urls SET in_sitemap=1 WHERE url=?", (url,))
                    break
            if not ready:
                log(f"[sitemaps] {site}: the sitemap does not list the new version yet (cached?); the daily run resubmits it")
        feed = meta.get("feed") or {}
        if feed.get("ok"):
            live = detect_feed(base, host)
            if live.get("ok") and set(live.get("links") or ()) & set(wanted):
                ready.add(feed["url"])
        if ready:
            out[site] = ready
    db.commit()
    return out


# ---------------------------------------------------------------- WebSub

def ping_hubs(db, sites, dry_run=False, only=None):
    """Notify the feed's WebSub hub(s) that the feed changed. Only for sites whose feed declares a hub, and only when
    URLs were added or updated since the last ping (or, on a fresh tick, when the live feed lists the new post)."""
    pinged = 0
    for site in sites:
        meta = store.get_meta(db, site)
        feed = meta.get("feed") or {}
        if not feed.get("ok") or not feed.get("hubs"):
            continue
        if only is not None and feed.get("url") not in only.get(site, ()):
            continue
        last = meta.get("websub_at")
        if last and (days_since(last) or 0) * 24 * 60 < config.WEBSUB_MIN_MINUTES:
            continue
        if only is None and last and not changes_since(db, site, last, use_lastmod=lastmod_trusted(meta)):
            continue
        if dry_run:
            log(f"[websub] {site}: would notify {len(feed['hubs'])} hub(s) that {feed['url']} changed")
            continue
        ok = 0
        for hub in feed["hubs"]:
            try:
                r = http().post(hub, data={"hub.mode": "publish", "hub.url": feed["url"]}, timeout=(10, 30))
                code = r.status_code
            except Exception as e:
                code = type(e).__name__
            ok += isinstance(code, int) and 200 <= code <= 299
            log(f"[websub] {site}: {hub} told that {feed['url']} changed -> {code}")
        if ok:
            store.set_meta(db, site, websub_at=now_iso())
            pinged += 1
    return pinged
