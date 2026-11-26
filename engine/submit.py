# -*- coding: utf-8 -*-
"""Stage 4 - push URLs through the channels that accept them.

Rules that keep this from being spam:
  * only URLs that are indexable right now (200, no redirect, no noindex, canonical to self, robots allow);
  * each URL once per channel, then again only if its lastmod moved, or (unchanged) after
    RESUBMIT_UNCHANGED_DAYS when Bing still reports it uncrawled;
  * IndexNow first (one call reaches Bing and the other IndexNow engines), the Bing API when a site
    has no IndexNow key or IndexNow refuses the batch, both with --both.

The Bing API gives each site a daily and a monthly allowance (100 a day for a new site). When it runs out
the rest waits, and the log says how many wait and for how long; IndexNow has no such allowance.
"""
import os
import re
import sqlite3
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


def _latest_successes(db, rows, channel):
    """Fetch the same winning submission as the per-URL query, in bounded batches."""
    urls = list(dict.fromkeys(r["url"] for r in rows))
    if not urls:
        return {}
    # Leave room for the channel parameter, including on connections with a lowered limit.
    limit = db.getlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER)
    if limit == 1:
        # quote() alone truncates TEXT at embedded NULs; encode it as a hex blob.
        channel_sql = db.execute("SELECT CASE typeof(?1) WHEN 'text' THEN "
                                 "'CAST(X''' || hex(?1) || ''' AS TEXT)' "
                                 "ELSE quote(?1) END", {"1": channel}).fetchone()[0]
        channel_params = ()
    else:
        channel_sql = "?"
        channel_params = (channel,)
    chunk_size = min(900, max(1, limit - len(channel_params)))
    latest = {}
    for start in range(0, len(urls), chunk_size):
        chunk = urls[start:start + chunk_size]
        placeholders = ",".join("(?)" for _ in chunk)
        query = f"""
            WITH requested(url) AS (VALUES {placeholders})
            SELECT requested.url AS requested_url, s.rowid AS submission_rowid,
                   s.submitted_at, s.lastmod
            FROM requested LEFT JOIN submissions s ON s.rowid=(
                SELECT rowid FROM submissions
                WHERE url=requested.url AND channel={channel_sql} AND http_status BETWEEN 200 AND 299
                ORDER BY submitted_at DESC, rowid DESC LIMIT 1
            )"""
        for last in db.execute(query, (*chunk, *channel_params)):
            if last["submission_rowid"] is not None:
                latest[last["requested_url"]] = last
    return latest


def due(db, rows, channel):
    rows = list(rows)
    latest = _latest_successes(db, rows, channel)
    out = []
    for r in rows:
        # Within each tier, URLs Bing has never shown come first: on a tight quota they gain the most.
        shown = 1 if r["bing_impr"] else 0
        last = latest.get(r["url"])
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


def _bing_sent_today(db, site):
    """URLs Bing's API accepted for this site since 00:00 UTC, when its daily allowance starts again."""
    return db.execute("""SELECT COUNT(*) FROM submissions s JOIN urls u ON u.url=s.url
                         WHERE u.site=? AND s.channel='bing' AND s.http_status=200 AND s.submitted_at>=?""",
                      (site, now_iso()[:10])).fetchone()[0]


def bing_wait_note(left, sent_today, daily, monthly):
    """One sentence on the URLs the Bing API could not take: how many wait, why, and for how long."""
    if daily is None:
        return f"{left} URLs wait: Bing's allowance could not be read"
    why = (f"today's Bing allowance is used ({sent_today} sent today)" if sent_today
           else "Bing gives this site no URL allowance today")
    note = f"{why}; {left} URLs wait"
    if monthly is not None and left > monthly:
        note += f", more than the {monthly} left in this month's allowance"
    elif sent_today:
        days = -(-left // sent_today)
        note += f", about {days} more day{'s' if days != 1 else ''} at this rate"
    return note + ". An IndexNow key file removes the limit (README: IndexNow setup)"


def run(db, sites, bing_client=None, bing_sites=None, indexnow_client=None, dry_run=False, both=False,
        include_taxonomy=False, max_per_site=None):
    bing_sites = bing_sites or {}
    indexnow_client = indexnow_client or indexnow.IndexNowClient()
    cap = max_per_site if max_per_site is not None else 10**9
    summary = {}
    for site in sites:
        rows = indexable_rows(db, site, include_taxonomy)
        sent = {}
        key = indexnow.detect_key(db, site)
        can_bing = bool(bing_client and site in bing_sites)
        use_bing = can_bing and (both or not key)
        due_now = {ch: due(db, rows, ch) for ch in (["indexnow"] if key else []) + (["bing"] if use_bing else [])}
        passed = None
        if site in config.SITE_GATES and any(due_now.values()):
            union = list({u: lm for items in due_now.values() for u, lm in items}.items())
            passed = {u for u, _ in site_gate(site, union)}
            due_now = {ch: [(u, lm) for u, lm in items if u in passed] for ch, items in due_now.items()}
        if key:
            host, k, loc = key
            items = due_now["indexnow"][: min(cap, config.INDEXNOW_MAX_PER_POST)]
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
                if not ok and can_bing and not use_bing:
                    # IndexNow refused the batch: the Bing API still reaches Bing today, within its allowance.
                    use_bing = True
                    due_now["bing"] = [(u, lm) for u, lm in due(db, rows, "bing") if passed is None or u in passed]
                    log(f"[submit] {site}: falling back to the Bing URL Submission API for this run")
                left = len(due_now["indexnow"]) - len(items)
                if ok and left > 0 and cap >= config.INDEXNOW_MAX_PER_POST:
                    log(f"[submit] {site}: {left} more URLs go out on the next runs ({config.INDEXNOW_MAX_PER_POST} per IndexNow call)")
            else:
                log(f"[submit] {site}: IndexNow - nothing new or changed to send")
        elif not use_bing:
            log(f"[submit] {site}: no IndexNow key file and no Bing API access - skipped (see README: IndexNow setup)")
        if use_bing:
            burl = bing_sites[site]
            waiting = due_now["bing"]
            daily = monthly = None
            if waiting:                                    # nothing to send: no reason to ask Bing anything
                try:
                    daily, monthly = bing_client.quota(burl)
                except Exception as e:
                    log(f"[submit] {site}: Bing quota check failed: {e}")
            items = waiting[: min(daily or 0, cap)]
            n_ok = 0
            if not waiting:
                log(f"[submit] {site}: Bing API - nothing new or changed to send")
            elif items and dry_run:
                log(f"[submit] {site}: would send {len(items)} URLs to the Bing URL Submission API (quota today {daily})")
            elif items:
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
            # Say what is left behind and why, unless the owner's own per-site limit is what held it back.
            if waiting and not dry_run and len(waiting) > len(items) and (daily is None or daily < min(len(waiting), cap)):
                log(f"[submit] {site}: {bing_wait_note(len(waiting) - n_ok, _bing_sent_today(db, site), daily, monthly)}")
        summary[site] = sent
    return summary
