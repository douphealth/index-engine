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
        if tree.tag.lower().endswith("sitemapindex"):
            for el in tree.iter(SM + "sitemap"):
                loc = (el.findtext(SM + "loc") or "").strip()
                if loc and loc not in seen:
                    queue.append(loc)
            continue
        kind = kind_from_sitemap(sm)
        # Only <url><loc>, never <image:loc> (image entries inflated page counts in an earlier export).
        for el in tree.iter(SM + "url"):
            loc = (el.findtext(SM + "loc") or "").strip()
            if not loc or ASSET_RE.search(urlsplit(loc).path) or not same_site(loc, site_host):
                continue
            key = norm(loc)
            lastmod = (el.findtext(SM + "lastmod") or "").strip() or None
            old = found.get(key)
            if old is None or (old[0] not in SPECIFIC and kind in SPECIFIC):
                found[key] = (kind, sm, lastmod or (old[2] if old else None))
    return found, seen, errors


def wp_published(base, site_host, max_pages=60):
    """Published posts and pages from the public WP REST API: {url: (kind, modified_gmt, date_gmt)}. None if not WordPress."""
    out = {}
    for typ in ("posts", "pages"):
        page = 1
        while page <= max_pages:
            try:
                r = get(f"{base}/wp-json/wp/v2/{typ}", params={"per_page": 100, "page": page, "_fields": "link,modified_gmt,date_gmt"},
                        timeout=(10, 90))
            except Exception as e:
                log(f"  WP REST {typ} p{page}: {e}")
                return out if out else None
            if r.status_code != 200 or "json" not in r.headers.get("Content-Type", ""):
                if page == 1 and typ == "posts":
                    return None          # not WordPress, or REST closed
                break
            try:
                items = r.json()
            except ValueError:
                break
            if not isinstance(items, list) or not items:
                break
            for it in items:
                link = it.get("link") if isinstance(it, dict) else None
                if link and same_site(link, site_host):
                    out[norm(link)] = (typ[:-1], it.get("modified_gmt"), it.get("date_gmt"))
            total = int(r.headers.get("X-WP-TotalPages", page) or page)
            if page >= total:
                break
            page += 1
    return out


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
        wp = wp_published(base, host) if use_wp else None
        now = now_iso()
        before = store.get_meta(db, site).get("sitemap_count") or 0
        if before >= 20 and len(found) < 0.9 * before:
            alerts.add(db, site, None, "sitemap_shrank", "high",
                       f"Sitemaps now list {len(found)} URLs, down from {before}. Check for a plugin, Worker or setting change.")

        if found:
            db.execute("UPDATE urls SET in_sitemap=0 WHERE site=?", (site,))
        for url, (kind, sm, lastmod) in found.items():
            db.execute("""INSERT INTO urls(url, site, kind, sitemap, lastmod, in_sitemap, first_seen, last_seen)
                          VALUES(?,?,?,?,?,1,?,?)
                          ON CONFLICT(url) DO UPDATE SET kind=excluded.kind, sitemap=excluded.sitemap,
                            lastmod=COALESCE(excluded.lastmod, urls.lastmod), in_sitemap=1, last_seen=excluded.last_seen""",
                       (url, site, kind, sm, lastmod, now, now))
        missing = 0
        if wp is not None:
            db.execute("UPDATE urls SET in_wp=0 WHERE site=?", (site,))
            for url, (kind, modified, published) in wp.items():
                if url not in found:
                    missing += 1
                db.execute("""INSERT INTO urls(url, site, kind, lastmod, in_wp, first_seen, last_seen, published)
                              VALUES(?,?,?,?,1,?,?,?)
                              ON CONFLICT(url) DO UPDATE SET in_wp=1, last_seen=excluded.last_seen,
                                lastmod=COALESCE(urls.lastmod, excluded.lastmod), published=excluded.published,
                                kind=CASE WHEN urls.kind IN ('post','page','product','taxonomy') THEN urls.kind ELSE excluded.kind END""",
                           (url, site, kind, modified, now, now, published))
        db.commit()
        store.set_meta(db, site, base=base, robots=robots, sitemap_roots=roots, sitemaps_read=read,
                       sitemap_errors=errors[:20], wp=wp is not None, wp_count=len(wp or {}),
                       sitemap_count=len(found), missing_from_sitemap=missing, discovered_at=now)
        log(f"  sitemaps read={len(read)} urls={len(found)} errors={len(errors)}"
            + (f" | WP published={len(wp)}, in no sitemap={missing} (the audit sorts noindexed from truly missing)"
               if wp is not None else " | not WordPress / REST closed"))
