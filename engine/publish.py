# -*- coding: utf-8 -*-
"""Shape one run's results for the dashboard: a portfolio summary (with the model's view, best next actions and
measured impact) plus one compact URL table per site."""
from collections import Counter

from . import config, report
from .triage import BLOCKERS

COLS = ["url", "bucket", "flags", "reason", "action", "value", "google", "bing", "kind", "in_sitemap", "words", "title",
        "odds", "why", "trust"]
KNOWN = ("INDEXED", "CRAWLED_NOT_INDEXED", "DISCOVERED_NOT_INDEXED", "UNKNOWN_TO_GOOGLE", "GOOGLE_STALE", "GOOGLE_PICKED_OTHER")


def build(db, results, generated_at, submitted=None, disabled=(), intel=None):
    """Return (summary, {site: site_table}, run_totals)."""
    submitted = submitted or {}
    disabled = set(disabled or ())
    intel = intel or {}
    sites, tables, requests_ = [], {}, []
    order = {s: i for i, s in enumerate(config.SITES)}
    for site in sorted(results, key=lambda s: order.get(s, len(order))):
        res = results[site]
        meta = res["meta"]
        snap = res["snapshot"]
        n_sub = db.execute("SELECT COUNT(DISTINCT url) FROM submissions s JOIN urls u USING(url) "
                           "WHERE u.site=? AND s.http_status BETWEEN 200 AND 299", (site,)).fetchone()[0]
        rows = db.execute("""SELECT t.url, t.bucket, t.flags, t.reason, t.action, t.value, t.google, t.bing, u.kind, u.in_sitemap,
                                    a.words, a.title, t.odds, t.why, a.trust
                             FROM triage t JOIN urls u ON u.url=t.url LEFT JOIN audits a ON a.url=t.url
                             WHERE t.site=? ORDER BY t.value DESC""", (site,)).fetchall()
        sites.append({
            "site": site, "urls": snap["total"], "counts": dict(res["counts"]), "conflicts": snap.get("conflicts", 0),
            "excluded": snap.get("excluded", 0), "bing_earners": snap["bing_earners"], "indexnow": meta.get("indexnow"),
            "gsc_property": meta.get("gsc_property"), "bing_site": meta.get("bing_site"), "submitted_total": n_sub,
            "disabled": site in disabled, "wp": bool(meta.get("wp")),
            "traffic": {k: v for k, v in (res.get("traffic") or {}).items() if k != "series"},
            "bing_health": res.get("health"), "data_health": res.get("data_health"),
            "revival_headline": (res.get("revival") or {}).get("headline"),
            "trust_risk": sum(1 for r in rows if "TRUST_RISK" in (r["flags"] or "")),
            "flags": dict(Counter(f for r in rows if r["bucket"] not in BLOCKERS and "IGNORED" not in (r["flags"] or "")
                                  for f in (r["flags"] or "").split(",") if f)),
            "potential": (intel.get("potential") or {}).get(site, {}),
            "diagnosis": [[k, m] for k, m in res["diagnosis"]],
            "sitemaps": [{k: sm.get(k) for k in ("path", "lastDownloaded", "errors", "warnings")} for sm in meta.get("gsc_sitemaps") or []],
        })
        tables[site] = {"site": site, "generated_at": generated_at, "cols": COLS,
                        "rows": [[r[c] if r[c] is not None else "" for c in COLS] for r in rows],
                        "plan": res["plan"][:200],
                        "traffic_series": (res.get("traffic") or {}).get("series") or [],
                        "revival": res.get("revival"),
                        "dups": [{"url": u, "similar_to": o, "estimated_overlap": j} for u, (o, j) in list(res["dups"].items())[:300]]}
        titles = {r["url"]: r["title"] for r in rows}
        odds = {r["url"]: r["odds"] for r in rows}
        for m in res["manual"]:
            requests_.append({"site": site, "url": m["url"], "bucket": m["bucket"], "value": m["value"], "odds": odds.get(m["url"]),
                              "title": titles.get(m["url"]) or "", "link": report.inspect_link(meta.get("gsc_property"), m["url"])})

    totals = {"urls": 0, "indexed": 0, "known": 0, "crawled": 0, "discovered": 0, "unknown": 0, "conflicts": 0,
              "excluded": 0, "bing_earners": 0, "submitted": sum(sum(v.values()) for v in submitted.values())}
    per_site = {}
    for s in sites:
        c = s["counts"]
        known = sum(c.get(k, 0) for k in KNOWN)
        per_site[s["site"]] = {"urls": s["urls"], "indexed": c.get("INDEXED", 0), "known": known}
        totals["urls"] += s["urls"]
        totals["indexed"] += c.get("INDEXED", 0)
        totals["known"] += known
        totals["crawled"] += c.get("CRAWLED_NOT_INDEXED", 0)
        totals["discovered"] += c.get("DISCOVERED_NOT_INDEXED", 0)
        totals["unknown"] += c.get("UNKNOWN_TO_GOOGLE", 0)
        totals["conflicts"] += s["conflicts"]
        totals["excluded"] += s["excluded"]
        totals["bing_earners"] += s["bing_earners"]
    imp = intel.get("impact") or {}
    totals["gained_30d"], totals["lost_30d"] = imp.get("gained", 0), imp.get("lost", 0)
    series = {}
    for site, res in results.items():
        if site in disabled:
            continue
        for d, bc, bi, gc, gi in (res.get("traffic") or {}).get("series") or []:
            t = series.setdefault(d, [0, 0, 0, 0])
            t[0] += bc; t[1] += bi; t[2] += gc; t[3] += gi
    portfolio_series = [[d, *v] for d, v in sorted(series.items())]
    summary = {"generated_at": generated_at, "sites": sites, "requests": requests_, "portfolio_series": portfolio_series,
               "model": intel.get("model"), "actions": intel.get("actions") or [], "impact": imp}
    return summary, tables, {"totals": totals, "sites": per_site}


def push(dash, db, results, generated_at, submitted, log_text, disabled=(), intel=None):
    """Upload everything; returns the run summary for the run history (or None if the upload failed)."""
    summary, tables, run_summary = build(db, results, generated_at, submitted, disabled, intel)
    for site, table in tables.items():
        dash.put(f"site:{site.split('://')[-1].rstrip('/')}", table)
    dash.put("summary", summary)
    dash.put("log", log_text[-200_000:])
    return run_summary
