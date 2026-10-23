# -*- coding: utf-8 -*-
"""Load an earlier export_gsc_bing.py export (CSV files) as a starting point.

Only fills gaps: an inspection is loaded only where none exists, and traffic only where no live
pull has run. Recent-impression evidence is never seeded (old data cannot prove a page is indexed today).
Live runs with credentials overwrite all of it.
"""
import csv
import json
from collections import defaultdict
from pathlib import Path

from . import store
from .util import log, norm


def _rows(path):
    if not path.exists():
        return []
    with open(path, encoding="utf-8-sig", errors="replace") as fh:
        return list(csv.DictReader(fh))


def run(db, folder):
    folder = Path(folder)
    meta = json.loads((folder / "export_meta.json").read_text(encoding="utf-8")) if (folder / "export_meta.json").exists() else {}
    at = meta.get("run_at", "2000-01-01T00:00:00")
    at = at if "+" in at or at.endswith("Z") else at + "+00:00"

    # Search Console property per site, so request links work before the first credentialed run.
    for prop in meta.get("gsc_sites", []):
        dom = prop.split(":", 1)[1] if prop.startswith("sc-domain:") else prop.split("://")[-1].strip("/")
        dom = dom[4:] if dom.startswith("www.") else dom
        if not store.get_meta(db, dom).get("gsc_property"):
            store.set_meta(db, dom, gsc_property=prop)

    n = 0
    for r in _rows(folder / "gsc_url_inspection.csv"):
        url = norm(r.get("url"))
        if not url or (r.get("error") and not r.get("coverageState")):
            continue
        cur = db.execute("INSERT OR IGNORE INTO gsc(url, property, inspected_at, verdict, coverage, indexing_state, fetch_state, "
                         "robots_state, last_crawl, google_canonical, user_canonical, error) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                         (url, "", at, r.get("verdict"), r.get("coverageState"), r.get("indexingState"), r.get("pageFetchState"),
                          r.get("robotsTxtState"), r.get("lastCrawlTime"), r.get("googleCanonical"), r.get("userCanonical"), None))
        n += cur.rowcount
    log(f"[seed] {n} URL inspections loaded (dated {at[:10]})")

    agg = defaultdict(lambda: [0, 0, None])
    for r in _rows(folder / "gsc_pages_90d.csv"):
        k = norm(r.get("page"))
        if k:
            agg[k][0] += int(float(r.get("clicks") or 0))
            agg[k][1] += int(float(r.get("impressions") or 0))
            agg[k][2] = r.get("site")
    g = 0
    for url, (c, i, site) in agg.items():
        cur = db.execute("INSERT INTO perf(url, site, gsc_clicks, gsc_impr, gsc_at) VALUES(?,?,?,?,?) "
                         "ON CONFLICT(url) DO UPDATE SET gsc_clicks=excluded.gsc_clicks, gsc_impr=excluded.gsc_impr, gsc_at=excluded.gsc_at "
                         "WHERE perf.gsc_at IS NULL OR perf.gsc_at <= excluded.gsc_at", (url, site, c, i, at))
        g += cur.rowcount

    bagg = defaultdict(lambda: [0, 0, None])
    for r in _rows(folder / "bing_pages.csv"):
        k = norm(r.get("key"))
        if k:
            bagg[k][0] += int(float(r.get("clicks") or 0))
            bagg[k][1] += int(float(r.get("impressions") or 0))
            bagg[k][2] = r.get("site")
    b = 0
    for url, (c, i, site) in bagg.items():
        cur = db.execute("INSERT INTO perf(url, site, bing_clicks, bing_impr, bing_at) VALUES(?,?,?,?,?) "
                         "ON CONFLICT(url) DO UPDATE SET bing_clicks=excluded.bing_clicks, bing_impr=excluded.bing_impr, bing_at=excluded.bing_at "
                         "WHERE perf.bing_at IS NULL OR perf.bing_at <= excluded.bing_at", (url, site, c, i, at))
        b += cur.rowcount
    db.commit()
    log(f"[seed] Google traffic for {g} pages and Bing traffic for {b} pages loaded (dated {at[:10]})")
