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

from . import alerts, config, store
from .util import get, log, norm, now_iso, same_site

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


def run(db, sites, use_wp=True):
    for site in sites:
        base = config.site_base(site)
        host = urlsplit(base).netloc
        log(f"[discover] {site}")
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
        sitemap_complete = bool(roots) and not errors
        wp, wp_complete = _wp_published_scan(base, host) if use_wp else (None, False)
        now = now_iso()
        before = store.get_meta(db, site).get("sitemap_count") or 0
        if sitemap_complete and before >= 20 and len(found) < 0.9 * before:
            alerts.add(db, site, None, "sitemap_shrank", "high",
                       f"Sitemaps now list {len(found)} URLs, down from {before}. Check for a plugin, Worker or setting change.")

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
        store.set_meta(db, site, base=base, robots=robots, sitemap_roots=roots, sitemaps_read=read,
                       sitemap_errors=errors[:20], sitemap_complete=sitemap_complete,
                       wp=(wp is not None or bool(previous_meta.get("wp"))), wp_complete=wp_complete, wp_count=wp_count,
                       sitemap_count=sitemap_count, missing_from_sitemap=missing, discovered_at=now)
        wp_status = (f"WP observed={len(wp or {})}, inventory={wp_count}, in no sitemap={missing}"
                     + ("" if wp_complete else " (incomplete)") if use_wp else "WP disabled")
        log(f"  sitemaps read={len(read)} urls={len(found)} errors={len(errors)}"
            + (" (incomplete)" if not sitemap_complete else "") + f" | {wp_status}")
