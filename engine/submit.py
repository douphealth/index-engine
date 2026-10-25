# -*- coding: utf-8 -*-
"""Stage 4 - push URLs through the channels that accept them.

Rules that keep this from being spam:
  * only URLs that are indexable right now (200, no redirect, no noindex, canonical to self, robots allow);
  * each URL once per channel, then again only if its lastmod moved, or (unchanged) after
    RESUBMIT_UNCHANGED_DAYS when Bing still reports it uncrawled;
  * IndexNow first (one call reaches Bing and the other IndexNow engines), the Bing API when a site
    has no IndexNow key, both with --both.
"""
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

from . import config, indexnow
from .triage import value_score
from .util import days_since, log, now_iso, parse_ts


def indexable_rows(db, site, include_taxonomy=False):
    rows = db.execute("""
        SELECT u.url, u.kind, u.lastmod, COALESCE(p.gsc_clicks,0) AS gsc_clicks, COALESCE(p.gsc_impr,0) AS gsc_impr,
               COALESCE(p.bing_clicks,0) AS bing_clicks, COALESCE(p.bing_impr,0) AS bing_impr, a.words,
               b.last_crawled AS bing_crawled
        FROM urls u JOIN audits a ON a.url=u.url LEFT JOIN perf p ON p.url=u.url LEFT JOIN bing_info b ON b.url=u.url
        LEFT JOIN owner_state o ON o.url=u.url
        WHERE u.site=? AND (u.in_sitemap=1 OR u.in_wp=1) AND a.status=200 AND a.hops=0 AND a.noindex=0
              AND COALESCE(o.ignored, 0)=0
              AND a.robots_bing=1 AND COALESCE(a.canonical_state,'') != 'other'""", (site,)).fetchall()
    return [r for r in rows if include_taxonomy or r["kind"] in config.SUBMIT_KINDS]


def due(db, rows, channel):
    out = []
    for r in rows:
        # Within each tier, URLs Bing has never shown come first: on a tight quota they gain the most.
        shown = 1 if r["bing_impr"] else 0
        last = db.execute("SELECT submitted_at, lastmod FROM submissions WHERE url=? AND channel=? AND http_status BETWEEN 200 AND 299 "
                          "ORDER BY submitted_at DESC, rowid DESC LIMIT 1", (r["url"], channel)).fetchone()
        if last is None:
            out.append((0, shown, -value_score(r), r["url"], r["lastmod"]))
            continue
        lm_new, lm_old = parse_ts(r["lastmod"]), parse_ts(last["lastmod"])
        if lm_new and (lm_old is None or lm_new > lm_old):
            out.append((1, shown, -value_score(r), r["url"], r["lastmod"]))
        elif not shown and not r["bing_crawled"] and (days_since(last["submitted_at"]) or 0) >= config.RESUBMIT_UNCHANGED_DAYS:
            out.append((2, shown, -value_score(r), r["url"], r["lastmod"]))
    out.sort()
    return [(u, lm) for _, _, _, u, lm in out]


def site_gate(site, items):
    """Run the site's own pre-submission gate when it has one; keep only its PASS lines. Fails closed."""
    gate = config.SITE_GATES.get(site)
    if not gate or not items:
        return items
    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False, encoding="utf-8") as fh:
        fh.write("\n".join(u for u, _ in items) + "\n")
        listfile = fh.name
    try:
        out = subprocess.run([sys.executable, str(gate), "gate", "--file", listfile], cwd=str(Path(gate).parent.parent),
                             capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=3600).stdout
    except Exception as e:
        log(f"[submit] {site}: site gate failed to run ({type(e).__name__}) - submitting nothing")
        return []
    finally:
        os.unlink(listfile)
    verdict = {m.group(2): m.group(1) for m in re.finditer(r"(?m)^(PASS|BLOCK|WAIT)\s+(\S+)", out)}
    passed = [(u, lm) for u, lm in items if verdict.get(u) == "PASS"]
    held = len(items) - len(passed)
    log(f"[submit] {site}: site gate {Path(gate).name}: {len(passed)} PASS, {held} held back (BLOCK/WAIT/no verdict)")
    return passed


def _record(db, items, channel, code):
    now = now_iso()
    db.executemany("INSERT INTO submissions(url, channel, submitted_at, lastmod, http_status) VALUES(?,?,?,?,?)",
                   [(u, channel, now, lm, code) for u, lm in items])
    db.commit()


def run(db, sites, bing_client=None, bing_sites=None, indexnow_client=None, dry_run=False, both=False,
        include_taxonomy=False, max_per_site=None):
    bing_sites = bing_sites or {}
    indexnow_client = indexnow_client or indexnow.IndexNowClient()
    summary = {}
    for site in sites:
        rows = indexable_rows(db, site, include_taxonomy)
        sent = {}
        key = indexnow.detect_key(db, site)
        use_bing = bing_client and site in bing_sites and (both or not key)
        due_now = {ch: due(db, rows, ch) for ch in (["indexnow"] if key else []) + (["bing"] if use_bing else [])}
        if site in config.SITE_GATES and any(due_now.values()):
            union = list({u: lm for items in due_now.values() for u, lm in items}.items())
            passed = {u for u, _ in site_gate(site, union)}
            due_now = {ch: [(u, lm) for u, lm in items if u in passed] for ch, items in due_now.items()}
        if key:
            host, k, loc = key
            items = due_now["indexnow"][: min((max_per_site if max_per_site is not None else 10**9), config.INDEXNOW_MAX_PER_POST)]
            if items and dry_run:
                log(f"[submit] {site}: would send {len(items)} URLs to IndexNow")
            elif items:
                code, text = indexnow_client.submit(host, k, loc, [u for u, _ in items])
                _record(db, items, "indexnow", code)
                ok = code in (200, 202)
                log(f"[submit] {site}: IndexNow {len(items)} URLs -> HTTP {code}{'' if ok else ' ' + text}")
                if code == 403:
                    log(f"  IndexNow rejected the key for {host}; check the key file is still served")
                sent["indexnow"] = len(items) if ok else 0
            else:
                log(f"[submit] {site}: IndexNow - nothing new or changed to send")
        elif not use_bing:
            log(f"[submit] {site}: no IndexNow key file and no Bing API access - skipped (see README: IndexNow setup)")
        if use_bing:
            burl = bing_sites[site]
            try:
                daily, _monthly = bing_client.quota(burl)
            except Exception as e:
                log(f"[submit] {site}: Bing quota check failed: {e}")
                daily = 0
            items = due_now["bing"][: min(daily, (max_per_site if max_per_site is not None else 10**9))]
            if items and dry_run:
                log(f"[submit] {site}: would send {len(items)} URLs to the Bing URL Submission API (quota today {daily})")
            elif items:
                n_ok = 0
                for i in range(0, len(items), config.BING_BATCH):
                    chunk = items[i:i + config.BING_BATCH]
                    code, text = bing_client.submit(burl, [u for u, _ in chunk])
                    _record(db, chunk, "bing", code)
                    if code != 200:
                        log(f"[submit] {site}: Bing SubmitUrlBatch HTTP {code} {text} - stopping")
                        break
                    n_ok += len(chunk)
                log(f"[submit] {site}: Bing API accepted {n_ok} URLs (daily quota {daily})")
                sent["bing"] = n_ok
            elif daily == 0:
                log(f"[submit] {site}: Bing daily URL quota is 0 or unknown")
        summary[site] = sent
    return summary
