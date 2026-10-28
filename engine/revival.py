# -*- coding: utf-8 -*-
"""Revival analysis: where did a site's search traffic go, and which pages are worth recovering first?

For every page that earned clicks in either of the last two 28-day windows (Bing and Google together) this splits the
change by what is behind it, so effort goes where it can work:

  blocked      the page is noindexed, redirected, canonical elsewhere, robots-blocked or erroring (a fix or a decision)
  gone / moved the URL earned clicks but is not in the sitemap or WordPress any more (404/410, or a redirect)
  unlisted     live, but missing from the sitemap and WordPress list
  live         live and indexable: the page itself lost visibility (ranking, demand, quality, links)

Everything is an association measured on the engines' own data. It does not say WHY a page lost clicks.
"""
import datetime as dt

import requests

from urllib.parse import urljoin, urlsplit

from . import config, traffic
from .triage import BLOCKERS
from .util import bare_host, log, norm, now_iso

MIN_LOSS = 3               # clicks a page must have lost to be listed
TOP = 40
PROBE_PER_SITE = 15

STATE_WORDS = {"INDEXED": "Google lists it", "CRAWLED_NOT_INDEXED": "Google crawled it but does not index it",
               "DISCOVERED_NOT_INDEXED": "Google knows the URL but has not crawled it",
               "UNKNOWN_TO_GOOGLE": "Google has never seen it", "GOOGLE_STALE": "Google last saw an older version",
               "GOOGLE_PICKED_OTHER": "Google chose another URL as the main copy", "NOT_INSPECTED": "No Google verdict yet"}
FLAG_WORDS = {"ORPHAN": "no page on the site links to it", "NO_BODY_LINKS": "linked only from menus or sidebars",
              "THIN": "thin content", "NEAR_DUPLICATE": "shares most of its text with another page",
              "DUPLICATE_TITLE": "its title duplicates another page's", "TRUST_RISK": "contains unverified first-hand claims",
              "SLOW": "slow to load", "SOFT_404": "reads like an error page"}


def _diagnose(kind, row, probe):
    """(label, what to do) for one losing page. Wording is deliberately about evidence, not cause."""
    prev = row["prev"]
    if kind == "live" and urlsplit(row.get("url") or "").path in ("", "/"):
        return ("home", f"This is your home page; it earned {prev} clicks in the earlier window. A home page is not linked from article text, so look "
                        "at what it ranks for (brand and head terms) in Bing Webmaster / Search Console, and at its title and description.")
    if kind == "blocked":
        why = (row["reason"] or row["bucket"] or "").strip()
        return ("blocked", f"Blocked ({why}). It earned {prev} clicks in the earlier window. If the block is deliberate, nothing to do; "
                           "otherwise fix it, or redirect to the closest page that keeps the traffic. Check clicks on both engines first (guardrail 1).")
    if kind == "gone":
        return ("gone", f"The URL now answers HTTP {probe['status']} but earned {prev} clicks. Restore the page, or 301-redirect it to its closest "
                        "replacement (only after checking both engines' clicks).")
    if kind == "moved":
        tgt = probe.get("target")
        seen = f" (that page earned {tgt['prev']} clicks before and {tgt['now']} now)" if tgt else ""
        return ("moved", f"The URL redirects to {probe['location'] or 'another address'}{seen}. Check that target is the page that should keep these clicks.")
    if kind == "unlisted":
        return ("unlisted", "The URL answers normally but is not in your sitemap or WordPress list, so the engine cannot track or submit it. "
                            "Find out why it is unlisted.")
    if kind == "unchecked":
        return ("unchecked", "Earned clicks but is unknown to your sitemap and WordPress list; its live status has not been checked yet.")
    bits = [STATE_WORDS.get(row["bucket"] or "", "")] + [FLAG_WORDS[f] for f in (row["flags"] or "").split(",") if f in FLAG_WORDS]
    bits = [b for b in bits if b]
    if any(f in (row["flags"] or "") for f in ("ORPHAN", "NO_BODY_LINKS")):
        todo = "Add contextual links from pages that rank (Links page)."
    elif "THIN" in (row["flags"] or "") or "NEAR_DUPLICATE" in (row["flags"] or ""):
        todo = "Make it clearly different and more substantial, or consolidate it after checking both engines' clicks."
    elif "TRUST_RISK" in (row["flags"] or ""):
        todo = "Source or remove the flagged claims."
    else:
        todo = ("It is live and technically fine, so the loss points to rankings or demand: compare its queries in Bing Webmaster "
                "(Search Performance, this page) and improve it with first-hand, sourced detail.")
    return ("live", ("; ".join(bits) + ". " if bits else "") + todo)


def _probes(db, site):
    return {r["url"]: dict(r) for r in db.execute(
        "SELECT p.url, p.status, p.location FROM trend_probe p JOIN page_trend t ON t.url=p.url WHERE t.site=?", (site,))}


def analyze(db, site, tr=None):
    """The revival picture for one site, from stored data only (cheap enough to run on every tick)."""
    tr = tr if tr is not None else traffic.site_traffic(db, site)
    rows = db.execute("""
        SELECT p.url, p.b_now, p.b_prev, p.bi_now, p.bi_prev, p.g_now, p.g_prev, p.gi_now, p.gi_prev,
               t.bucket, t.flags, t.reason, t.odds, a.title, a.words, u.lastmod, u.url IS NOT NULL AS known
        FROM page_trend p LEFT JOIN urls u ON u.url=p.url LEFT JOIN triage t ON t.url=p.url AND t.site=p.site
        LEFT JOIN audits a ON a.url=p.url WHERE p.site=?""", (site,)).fetchall()
    probes = _probes(db, site)
    trend = {r["url"]: (r["p"], r["n"]) for r in db.execute(
        "SELECT url, b_prev + g_prev AS p, b_now + g_now AS n FROM page_trend WHERE site=?", (site,))}
    for url, pr in probes.items():
        if pr.get("location"):
            t = trend.get(norm(urljoin(url, pr["location"])))
            pr["target"] = {"prev": t[0], "now": t[1]} if t else None
    by_kind, gained_by_kind = {}, {}
    page_now = page_prev = 0
    losers, gainers = [], []
    for r in rows:
        now, prev = (r["b_now"] or 0) + (r["g_now"] or 0), (r["b_prev"] or 0) + (r["g_prev"] or 0)
        page_now += now
        page_prev += prev
        if r["bucket"] is not None and r["bucket"] in BLOCKERS:
            kind = "blocked"
        elif r["bucket"] is not None:
            kind = "live"
        else:
            pr = probes.get(r["url"])
            kind = "unchecked" if not pr else "gone" if pr["status"] in (404, 410) else "moved" if 300 <= (pr["status"] or 0) < 400 \
                else "unlisted" if pr["status"] == 200 else "unchecked"
        delta = now - prev
        if delta < 0:
            b = by_kind.setdefault(kind, {"pages": 0, "clicks_lost": 0})
            b["pages"] += 1
            b["clicks_lost"] += -delta
        elif delta > 0:
            b = gained_by_kind.setdefault(kind, {"pages": 0, "clicks_gained": 0})
            b["pages"] += 1
            b["clicks_gained"] += delta
        item = {"url": r["url"], "title": r["title"] or "", "bucket": r["bucket"], "kind": kind, "prev": prev, "now": now, "delta": delta,
                "bing_prev": r["b_prev"] or 0, "bing_now": r["b_now"] or 0, "google_prev": r["g_prev"] or 0, "google_now": r["g_now"] or 0,
                "impr_prev": (r["bi_prev"] or 0) + (r["gi_prev"] or 0), "impr_now": (r["bi_now"] or 0) + (r["gi_now"] or 0),
                "lastmod": (r["lastmod"] or "")[:10], "odds": r["odds"]}
        if delta <= -MIN_LOSS:
            label, todo = _diagnose(kind, {"url": r["url"], "prev": prev, "bucket": r["bucket"], "flags": r["flags"], "reason": r["reason"]},
                                    probes.get(r["url"]) or {})
            item["diagnosis"], item["todo"] = label, todo
            losers.append(item)
        elif delta >= MIN_LOSS:
            gainers.append(item)
    losers.sort(key=lambda x: x["delta"])
    gainers.sort(key=lambda x: -x["delta"])
    variants = [dict(r) for r in db.execute(
        "SELECT v.variant, v.listed, q.status FROM url_variants v LEFT JOIN trend_probe q ON q.url=v.variant WHERE v.site=?", (site,))]
    dup = [v for v in variants if v["status"] == 200]
    checked = sum(1 for v in variants if v["status"])
    partial = f" (checked {checked} of {len(variants)} so far)" if checked < len(variants) else ""
    findings = []
    if len(dup) >= 3:
        findings.append({"kind": "duplicate_urls", "severity": "warn", "count": len(dup), "examples": [v["variant"] for v in dup[:5]],
                         "message": f"{len(dup)} URLs that earn clicks are answered at a second spelling of a listed page{partial} (for example without the "
                                    "trailing slash) with HTTP 200 instead of a 301 redirect. Search engines can treat these as duplicates and split clicks "
                                    "and links between them. Redirect the variants to the listed URL, or make sure the canonical points at it."})
    site_now = sum((tr.get(s) or {}).get("clicks_now", 0) for s in ("bing", "google"))
    site_prev = sum((tr.get(s) or {}).get("clicks_prev", 0) for s in ("bing", "google"))
    # Page-level reports omit some low-volume traffic, so say how much of the site's clicks the page rows explain.
    cover = lambda page, site_total: round(min(1.0, page / site_total), 3) if site_total else None  # noqa: E731
    return {"windows": {s: [(tr.get(s) or {}).get("now"), (tr.get(s) or {}).get("prev")] for s in ("bing", "google")},
            "site_clicks": {"now": site_now, "prev": site_prev}, "page_clicks": {"now": page_now, "prev": page_prev},
            "coverage": {"now": cover(page_now, site_now), "prev": cover(page_prev, site_prev)},
            "lost_by_kind": by_kind, "gained_by_kind": gained_by_kind,
            "pages_lost": sum(v["pages"] for v in by_kind.values()), "pages_gained": sum(v["pages"] for v in gained_by_kind.values()),
            "clicks_lost": sum(v["clicks_lost"] for v in by_kind.values()), "clicks_gained": sum(v["clicks_gained"] for v in gained_by_kind.values()),
            "variants": {"total": len(variants), "served_twice": len(dup), "redirected": sum(1 for v in variants if v["status"] and 300 <= v["status"] < 400)},
            "findings": findings, "losers": losers[:TOP], "gainers": gainers[:10]}


def probe_missing(db, sites, session=None, limit=PROBE_PER_SITE):  # noqa: C901
    """Ask the live site about URLs that earned clicks but that the engine does not know (read-only, a few per site).
    A removed or moved URL is the most recoverable kind of loss."""
    s = session or requests
    for site in sites:
        cand = db.execute("""SELECT v.variant AS url, 0 AS lost FROM url_variants v LEFT JOIN trend_probe q ON q.url=v.variant
                             WHERE v.site=? AND (q.at IS NULL OR julianday('now') - julianday(q.at) > 7) LIMIT ?""", (site, limit)).fetchall()
        cand += db.execute("""SELECT p.url, (p.b_prev + p.g_prev) - (p.b_now + p.g_now) AS lost FROM page_trend p
                             LEFT JOIN urls u ON u.url=p.url LEFT JOIN trend_probe q ON q.url=p.url
                             WHERE p.site=? AND u.url IS NULL AND lost >= 1 AND (q.at IS NULL OR julianday('now') - julianday(q.at) > 7)
                             ORDER BY lost DESC LIMIT ?""", (site, limit)).fetchall()
        for c in cand:
            if bare_host(urlsplit(c["url"]).netloc) != bare_host(site.split("://")[-1].split("/")[0]):
                continue                       # only ever ask the site's own host
            try:
                r = s.get(c["url"], allow_redirects=False, timeout=(8, 20), headers={"User-Agent": config.UA})
                status, loc = r.status_code, (r.headers.get("Location") or "")[:300]
            except Exception as e:
                status, loc = 0, type(e).__name__
            db.execute("INSERT OR REPLACE INTO trend_probe(url, status, location, at) VALUES(?,?,?,?)", (c["url"], status, loc, now_iso()))
        db.commit()
        if cand:
            log(f"[revival] {site}: checked {len(cand)} URLs that earned clicks but are unlisted, or spelling variants of listed pages")


def headline(site, tr, rev, health):
    """One honest sentence per site."""
    parts = []
    for src, name in (("bing", "Bing"), ("google", "Google")):
        t = tr.get(src)
        if not t or t["clicks"]["verdict"] == "too_little_data":
            continue
        v = t["clicks"]
        word = {"declining": "fell", "growing": "rose", "stable": "were steady"}[v["verdict"]]
        parts.append(f"{name} clicks {word}" + (f" {abs(v['pct']):.0f}% ({t['clicks_prev']:,} to {t['clicks_now']:,})" if v["verdict"] != "stable" else f" ({t['clicks_prev']:,} to {t['clicks_now']:,})"))
    return "; ".join(parts) if parts else "Too little traffic in either window to call a trend."


def _recent(db, site, kind, days):
    return db.execute("SELECT 1 FROM alerts WHERE site=? AND kind=? AND julianday('now') - julianday(at) < ?", (site, kind, days)).fetchone() is not None


def raise_alerts(db, results, only=None):
    """Alerts for clear traffic changes (weekly at most) and Bing health findings (monthly at most: they are chronic
    conditions, and repeating them weekly would bury the news)."""
    from . import alerts
    n = 0
    for site, res in results.items():
        if only is not None and site not in only:
            continue                           # paused sites get no alerts
        tr, health = res.get("traffic") or {}, res.get("health")
        for src, name in (("bing", "Bing"), ("google", "Google")):
            t = tr.get(src)
            if not t:
                continue
            v = t["clicks"]
            if v["verdict"] == "declining":
                sev = "high" if (v["pct"] or 0) <= -40 and t["clicks_prev"] >= 300 else "warn"
                n += alerts.add(db, site, None, f"traffic_drop_{src}", sev,
                                f"{name} clicks fell {abs(v['pct']):.0f}% ({t['clicks_prev']:,} to {t['clicks_now']:,}) in the 28 days to {t['end']} "
                                f"compared with the 28 before ({v['confidence']} confidence). See Revival for the pages behind it.")
            elif v["verdict"] == "growing":
                n += alerts.add(db, site, None, f"traffic_up_{src}", "good",
                                f"{name} clicks rose {v['pct']:.0f}% ({t['clicks_prev']:,} to {t['clicks_now']:,}) in the 28 days to {t['end']}.")
        for f in ((health or {}).get("findings", []) + (res.get("revival") or {}).get("findings", [])):
            if f["kind"] == "duplicate_urls":
                if not _recent(db, site, "revival_duplicate_urls", 30):
                    n += alerts.add(db, site, None, "revival_duplicate_urls", f["severity"], f["message"])
                continue
            if not _recent(db, site, f"bing_{f['kind']}", 30):
                n += alerts.add(db, site, None, f"bing_{f['kind']}", f["severity"], f["message"])
    db.commit()
    return n
