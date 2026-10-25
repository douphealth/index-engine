# -*- coding: utf-8 -*-
"""Fresh-content autopilot: on every 30-minute tick, find WordPress posts and pages published or updated since the
last look, and get them to the search engines within minutes: audit -> Google inspection -> site gate -> Bing/IndexNow.
One small REST call per site per tick when nothing changed."""
import datetime as dt

from . import audit, config, store
from .util import get, log, norm, now_iso, parse_ts


def changed(db, sites):
    """Find recent WordPress changes. The watermark advances only after successful processing."""
    out = {}
    for site in sites:
        meta = store.get_meta(db, site)
        if not meta.get("wp"):
            continue
        base = meta.get("base") or config.site_base(site)
        wm = parse_ts(meta.get("fresh_watermark"))
        if wm is None:
            store.set_meta(db, site, fresh_watermark=now_iso())
            continue
        since = (wm - dt.timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%S")   # WP compares in site time; margin, then exact filter
        newest, items = wm, []
        for typ in ("posts", "pages"):
            try:
                r = get(f"{base}/wp-json/wp/v2/{typ}", timeout=(10, 60),
                        params={"modified_after": since, "per_page": 100, "orderby": "modified",
                                "_fields": "link,modified_gmt,date_gmt", "_ie": int(dt.datetime.now().timestamp())})
            except Exception as e:
                log(f"[fresh] {site}: {typ} check failed: {type(e).__name__}")
                continue
            if r.status_code != 200 or "json" not in r.headers.get("Content-Type", ""):
                continue
            for it in r.json() if isinstance(r.json(), list) else []:
                mod = parse_ts((it.get("modified_gmt") or "") + "+00:00")
                if mod and mod > wm and it.get("link"):
                    items.append((norm(it["link"]), typ[:-1], it["modified_gmt"] + "+00:00", it.get("date_gmt")))
                    newest = max(newest, mod)
        # Never acknowledge a partial or failed scan; the next tick must retry it.
        if items:
            out[site] = items
        elif newest > wm:
            store.set_meta(db, site, fresh_watermark=newest.isoformat())
    return out


def ingest(db, changes):
    """Record and audit the changed URLs now. Returns the list of URLs."""
    urls = []
    for site, items in changes.items():
        rp = audit.robots_parser(store.get_meta(db, site).get("robots", ""))
        for url, kind, modified, published in items:
            db.execute("""INSERT INTO urls(url, site, kind, lastmod, in_wp, first_seen, last_seen, published)
                          VALUES(?,?,?,?,1,?,?,?)
                          ON CONFLICT(url) DO UPDATE SET in_wp=1, lastmod=excluded.lastmod, last_seen=excluded.last_seen,
                            published=COALESCE(urls.published, excluded.published)""",
                       (url, site, kind, modified, now_iso(), now_iso(), published))
            audit.save(db, audit.fetch_page(url, rp), modified)
            urls.append(url)
        log(f"[fresh] {site}: {len(items)} new or updated: " + ", ".join(u for u, *_ in items[:5]) + (" ..." if len(items) > 5 else ""))
    db.commit()
    return urls

def acknowledge(db, changes):
    """Commit per-site watermarks only after ingest, inspection and submission stages succeed."""
    for site, items in changes.items():
        times = [parse_ts(mod) for _, _, mod, _ in items]
        times = [t for t in times if t is not None]
        if times:
            old = parse_ts(store.get_meta(db, site).get("fresh_watermark"))
            newest = max(times)
            if old is None or newest > old:
                store.set_meta(db, site, fresh_watermark=newest.isoformat())
