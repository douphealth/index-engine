# -*- coding: utf-8 -*-
"""Stage 1 - find every URL a site wants indexed.

Sources: robots.txt Sitemap lines (recursing through sitemap indexes) and, on WordPress, the
public REST API for published posts and pages. A published post missing from every sitemap is
a discovery gap that no amount of "request indexing" fixes.
"""
import gzip
import re
import xml.etree.ElementTree as ET
from urllib.parse import urlsplit

from . import alerts, audit, config, sitemaps, store
from .util import fetched, get, log, norm, now_iso, parse_ts, same_site

SM = "{http://www.sitemaps.org/schemas/sitemap/0.9}"
ASSET_RE = re.compile(r"\.(jpe?g|png|webp|gif|svg|avif|pdf|mp4|webm|zip|css|js)$", re.I)
SPECIFIC = ("post", "page", "product", "taxonomy")


def kind_from_sitemap(sitemap_url):
    name = urlsplit(sitemap_url).path.lower().rsplit("/", 1)[-1]
    if re.search(r"category|tag|author|users|taxonom|product_cat|format|series", name):
        return "taxonomy"
    if "product" in name:
        return "product"
    if "page" in name:
        return "page"
    if "post" in name:
        return "post"
    return "other"


def fetch_robots(base):
    try:
        r = get(base + "/robots.txt", timeout=(10, 30))
        if r.status_code == 200 and "<html" not in r.text[:500].lower():
            return r.text
    except Exception as e:
        log(f"  robots.txt fetch failed: {e}")
    return ""


def robots_sitemaps(robots_txt):
    return [m.group(1).strip() for m in re.finditer(r"(?im)^\s*sitemap\s*:\s*(\S+)", robots_txt or "")]


def _xml(url):
    r = get(url, timeout=(10, 60))
    if r.status_code != 200:
        raise RuntimeError(f"HTTP {r.status_code}")
    body = r.content
    if url.endswith(".gz") or body[:2] == b"\x1f\x8b":
        body = gzip.decompress(body)
    return ET.fromstring(body)


def walk_sitemaps(base, roots, site_host, cap=300):
    """Return ({url: (kind, sitemap, lastmod)}, sitemaps_read, errors)."""
    found, seen, errors = {}, [], []
    queue = list(dict.fromkeys(roots))
    while queue and len(seen) < cap:
        sm = queue.pop(0)
        if sm in seen:
            continue
        seen.append(sm)
        try:
            tree = _xml(sm)
        except Exception as e:
            errors.append(f"{sm}: {e}")
            continue
        if tree.tag not in (SM + "sitemapindex", SM + "urlset", "sitemapindex", "urlset"):
            errors.append(f"{sm}: unexpected XML root {tree.tag}")
            continue
        ns = SM if tree.tag.startswith(SM) else ""
        if tree.tag.endswith("sitemapindex"):
            for el in tree.iter(ns + "sitemap"):
                loc = (el.findtext(ns + "loc") or "").strip()
                if loc and loc not in seen:
                    queue.append(loc)
                elif not loc:
                    errors.append(f"{sm}: sitemap entry without loc")
            continue
        kind = kind_from_sitemap(sm)
        # Only <url><loc>, never <image:loc> (image entries inflated page counts in an earlier export).
        for el in tree.iter(ns + "url"):
            loc = (el.findtext(ns + "loc") or "").strip()
            if not loc:
                errors.append(f"{sm}: URL entry without loc")
                continue
            if ASSET_RE.search(urlsplit(loc).path) or not same_site(loc, site_host):
                continue
            key = norm(loc)
            lastmod = (el.findtext(ns + "lastmod") or "").strip() or None
            old = found.get(key)
            if old is None or (old[0] not in SPECIFIC and kind in SPECIFIC):
                found[key] = (kind, sm, lastmod or (old[2] if old else None))
    if any(sm not in seen for sm in queue):
        errors.append(f"sitemap limit reached ({cap})")
    return found, seen, errors


def wp_published(base, site_host, max_pages=60):
    """Published posts and pages from the public WP REST API: {url: (kind, modified_gmt, date_gmt)}. None if not WordPress."""
    return _wp_published_scan(base, site_host, max_pages)[0]


def _wp_published_scan(base, site_host, max_pages=60):
    """Return the observed URLs and whether both collections were fully scanned."""
    out = {}
    for typ in ("posts", "pages"):
        page = 1
        while page <= max_pages:
            try:
                r = get(f"{base}/wp-json/wp/v2/{typ}", params={"per_page": 100, "page": page, "_fields": "link,modified_gmt,date_gmt"},
                        timeout=(10, 90))
            except Exception as e:
                log(f"  WP REST {typ} p{page}: {e}")
                return out or None, False
            if r.status_code != 200 or "json" not in r.headers.get("Content-Type", ""):
                log(f"  WP REST {typ} p{page}: HTTP {r.status_code} or non-JSON response")
                return out or None, False
            try:
                items = r.json()
            except ValueError:
                log(f"  WP REST {typ} p{page}: invalid JSON")
                return out or None, False
            if not isinstance(items, list):
                log(f"  WP REST {typ} p{page}: unexpected response")
                return out or None, False
            invalid_item = False
            for it in items:
                link = it.get("link") if isinstance(it, dict) else None
                if not isinstance(link, str) or not link:
                    invalid_item = True
                    continue
                if link and same_site(link, site_host):
                    out[norm(link)] = (typ[:-1], it.get("modified_gmt"), it.get("date_gmt"))
            if invalid_item:
                log(f"  WP REST {typ} p{page}: item without link")
                return out or None, False
            raw_total = r.headers.get("X-WP-TotalPages")
            try:
                total = int(raw_total) if raw_total not in (None, "") else None
                if total is not None and (total < 0 or (total == 0 and (items or page > 1)) or (total > 0 and page > total)):
                    raise ValueError("inconsistent page count")
            except (TypeError, ValueError):
                log(f"  WP REST {typ} p{page}: invalid page count")
                return out or None, False
            if not items:
                if total is not None and page < total:
                    log(f"  WP REST {typ} p{page}: empty page before final page")
                    return out or None, False
                break
            if total is None:
                # Without a total, a full page cannot establish that there are no more.
                if len(items) < 100:
                    break
                page += 1
                continue
            if page >= total:
                break
            page += 1
        else:
            log(f"  WP REST {typ}: page limit reached ({max_pages})")
            return out or None, False
    return out, True


HISTORY_KEEP = 12           # sitemap readings kept per site
SAMPLE = 12                 # dropped pages fetched to tell a deliberate removal from a broken sitemap
SWING = 0.10                # a change of more than this share (and of at least 20 URLs) counts as a swing
GROWTH_FACTOR, GROWTH_MIN = 3, 300


def unstable(history, now=None, window_days=3):
    """True when the number of listed URLs has swung back and forth: among the last six readings of the last three
    days, at least two changes of direction, each move larger than SWING. One drop or one jump is not this."""
    now = parse_ts(now) if now else None
    recent = [n for at, n in history[-6:]
              if not now or not parse_ts(at) or (now - parse_ts(at)).total_seconds() <= window_days * 86400]
    moves = []
    for a, b in zip(recent, recent[1:]):
        if abs(b - a) >= 20 and abs(b - a) > SWING * max(a, b):
            moves.append(1 if b > a else -1)
    return sum(1 for x, y in zip(moves, moves[1:]) if x != y) >= 2


def spread(urls, k=SAMPLE):
    """Up to k of the URLs, evenly spaced through the sorted list, so one section of the site cannot fill the sample."""
    urls = sorted(urls)
    if len(urls) <= k:
        return urls
    return [urls[int(i * len(urls) / k)] for i in range(k)]


def page_state(url, rp):
    """True when the page still answers and is indexable, False when it is gone on purpose (404/410, noindex, a
    redirect, a canonical elsewhere), None when it cannot be told (no answer, 429, 5xx)."""
    rec = audit.fetch_page(url, rp)
    status = rec["status"]
    if status is None or status == 429 or status >= 500:
        return None
    return audit._indexable(status, rec["hops"], rec["noindex"], rec["canonical_state"], rec["robots_google"])


def judge_sitemap(db, site, before, found, removed, history, robots=""):
    """Raise at most one alert about how the sitemap changed since the last complete reading.
    unstable  it keeps swinging between sizes (the site fails part of the time)
    pruned    it lost over 10% and the dropped pages checked are gone or noindex: deliberate, for information only
    shrank    it lost over 10% and dropped pages still answer and are indexable: the sitemap lost them, the site did not
    grew      it more than tripled (by at least 300 URLs)"""
    n = len(found)
    if unstable(history, history[-1][0] if history else None):
        seen = ", ".join(f"{c:,}" for _, c in history[-4:])
        alerts.add(db, site, None, "sitemap_unstable", "high",
                   f"The sitemaps keep changing size: {seen} URLs at the last readings. A sitemap that swings like this "
                   "usually means the site's database or build fails part of the time, so crawlers see pages vanish and "
                   "come back. Check the site's error log for the hours it was short.")
        return "unstable"
    if before >= 20 and n < (1 - SWING) * before:
        rp = audit.robots_parser(robots)
        states = [page_state(u, rp) for u in spread(removed)]
        known = [s for s in states if s is not None]
        alive = sum(known)
        if known and alive <= 0.2 * len(known):
            alerts.add(db, site, None, "sitemap_pruned", "info",
                       f"{len(removed):,} URLs left the sitemaps ({before:,} to {n:,}). {len(known) - alive} of the {len(known)} "
                       "checked now carry noindex, redirect or no longer answer, so this looks deliberate. Nothing to do if it was.")
            return "pruned"
        detail = (f" {alive} of the {len(known)} dropped pages checked still answer and are indexable: they left the "
                  "sitemap but not the site." if known else "")
        alerts.add(db, site, None, "sitemap_shrank", "high",
                   f"Sitemaps now list {n:,} URLs, down from {before:,}.{detail} Check for a plugin, Worker or setting change.")
        return "shrank"
    if before >= 20 and n >= GROWTH_FACTOR * before and n - before >= GROWTH_MIN:
        cap = config.AUDIT_SITE_MAX.get(site, config.AUDIT_MAX_PER_SITE)
        alerts.add(db, site, None, "sitemap_grew", "warn",
                   f"Sitemaps now list {n:,} URLs, up from {before:,}. If that was not a planned launch, check what generates "
                   f"the new URLs. The engine reads at most {cap} of this site's pages per run, so it is not flooded.")
        return "grew"
    return None


def read_site(site, use_wp=True, was_wp=False):
    """Everything discovery reads from one site, with no database access: robots.txt, every sitemap, the WordPress
    inventory and the feed. Sites are read side by side (util.fetched); run() stores each one in turn."""
    base = config.site_base(site)
    host = urlsplit(base).netloc
    robots = fetch_robots(base)
    roots = robots_sitemaps(robots)
    if not roots:
        for cand in ("/sitemap_index.xml", "/sitemap.xml", "/wp-sitemap.xml"):
            try:
                r = get(base + cand, timeout=(10, 30))
                if r.status_code == 200 and r.text.lstrip()[:1] == "<":
                    roots = [base + cand]
                    break
            except Exception:
                pass
    found, read, errors = walk_sitemaps(base, roots, host)
    wp, wp_complete = _wp_published_scan(base, host) if use_wp else (None, False)
    feed = sitemaps.feed_meta(sitemaps.detect_feed(base, host)) if use_wp and (wp is not None or was_wp) else None
    return {"base": base, "robots": robots, "roots": roots, "found": found, "read": read, "errors": errors,
            "wp": wp, "wp_complete": wp_complete, "feed": feed}


def run(db, sites, use_wp=True):
    was_wp = {site: bool(store.get_meta(db, site).get("wp")) for site in sites}
    for site, got, err in fetched(sites, lambda s: read_site(s, use_wp, was_wp[s])):
        log(f"[discover] {site}")
        if err:
            log(f"  discovery failed ({type(err).__name__}: {str(err)[:120]}); what is stored for this site is kept")
            continue
        base, robots, roots = got["base"], got["robots"], got["roots"]
        found, read, errors, wp, wp_complete = got["found"], got["read"], got["errors"], got["wp"], got["wp_complete"]
        sitemap_complete = bool(roots) and not errors
        now = now_iso()
        meta_before = store.get_meta(db, site)
        before = meta_before.get("sitemap_count") or 0
        history = list(meta_before.get("sitemap_history") or [])
        if sitemap_complete:
            # Only a complete reading is compared and remembered: a sitemap that failed to load says nothing about size.
            history = (history + [[now, len(found)]])[-HISTORY_KEEP:]
            removed = [r["url"] for r in db.execute("SELECT url FROM urls WHERE site=? AND in_sitemap=1", (site,))
                       if r["url"] not in found]
            verdict = judge_sitemap(db, site, before, found, removed, history, robots)
            if verdict:
                log(f"  sitemap {verdict}: {before} -> {len(found)} URLs")

        if sitemap_complete:
            db.execute("UPDATE urls SET in_sitemap=0 WHERE site=?", (site,))
        for url, (kind, sm, lastmod) in found.items():
            db.execute("""INSERT INTO urls(url, site, kind, sitemap, lastmod, in_sitemap, first_seen, last_seen)
                          VALUES(?,?,?,?,?,1,?,?)
                          ON CONFLICT(url) DO UPDATE SET kind=excluded.kind, sitemap=excluded.sitemap,
                            lastmod=COALESCE(excluded.lastmod, urls.lastmod), in_sitemap=1, last_seen=excluded.last_seen""",
                       (url, site, kind, sm, lastmod, now, now))
        if wp is not None:
            if wp_complete:
                db.execute("UPDATE urls SET in_wp=0 WHERE site=?", (site,))
            for url, (kind, modified, published) in wp.items():
                db.execute("""INSERT INTO urls(url, site, kind, lastmod, in_wp, first_seen, last_seen, published)
                              VALUES(?,?,?,?,1,?,?,?)
                              ON CONFLICT(url) DO UPDATE SET in_wp=1, last_seen=excluded.last_seen,
                                lastmod=COALESCE(urls.lastmod, excluded.lastmod), published=excluded.published,
                                kind=CASE WHEN urls.kind IN ('post','page','product','taxonomy') THEN urls.kind ELSE excluded.kind END""",
                           (url, site, kind, modified, now, now, published))
        counts = db.execute("SELECT COALESCE(SUM(in_sitemap), 0), COALESCE(SUM(in_wp), 0), "
                            "COALESCE(SUM(CASE WHEN in_wp=1 AND in_sitemap=0 THEN 1 ELSE 0 END), 0) "
                            "FROM urls WHERE site=?", (site,)).fetchone()
        sitemap_count, wp_count, missing = counts
        db.commit()
        previous_meta = store.get_meta(db, site)
        is_wp = wp is not None or bool(previous_meta.get("wp"))
        extra = {}
        if found:
            extra["lastmod_health"] = sitemaps.lastmod_health(found, wp)
        if got["feed"] is not None:
            extra["feed"] = got["feed"]
        store.set_meta(db, site, **extra, base=base, robots=robots, sitemap_roots=roots, sitemaps_read=read,
                       sitemap_errors=errors[:20], sitemap_complete=sitemap_complete, sitemap_history=history,
                       wp=is_wp, wp_complete=wp_complete, wp_count=wp_count,
                       sitemap_count=sitemap_count, missing_from_sitemap=missing, discovered_at=now)
        wp_status = (f"WP observed={len(wp or {})}, inventory={wp_count}, in no sitemap={missing}"
                     + ("" if wp_complete else " (incomplete)") if use_wp else "WP disabled")
        log(f"  sitemaps read={len(read)} urls={len(found)} errors={len(errors)}"
            + (" (incomplete)" if not sitemap_complete else "") + f" | {wp_status}")
        feed, health = extra.get("feed"), extra.get("lastmod_health")
        if feed:
            log(f"  feed {feed['url']}: " + (f"{feed['items']} posts, WebSub hubs: {len(feed['hubs'])}" if feed["ok"] else f"not usable ({feed['why']})"))
        if health and not sitemaps.lastmod_trusted({"lastmod_health": health}):
            log("  sitemap lastmod is not trustworthy (same value on most URLs, or in the future): updates are detected from new URLs only")
