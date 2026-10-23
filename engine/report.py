# -*- coding: utf-8 -*-
"""Stage 6 - write the CSV work lists and one self-contained HTML report to out/."""
import csv
import datetime as dt
import html
import json
from urllib.parse import quote

from . import config
from .triage import BLOCKERS

NOT_INDEXED = ("CRAWLED_NOT_INDEXED", "DISCOVERED_NOT_INDEXED", "UNKNOWN_TO_GOOGLE", "GOOGLE_STALE", "GOOGLE_PICKED_OTHER")
LABEL = {
    "INDEXED": "Indexed by Google", "CRAWLED_NOT_INDEXED": "Crawled, not indexed", "DISCOVERED_NOT_INDEXED": "Discovered, not crawled",
    "UNKNOWN_TO_GOOGLE": "Unknown to Google", "GOOGLE_STALE": "Fixed since Google's last look", "GOOGLE_PICKED_OTHER": "Google picked another URL",
    "NOT_INSPECTED": "No Google data yet", "NOT_CHECKED": "Not fetched yet", "FETCH_FAILED": "Fetch failed", "BROKEN": "Broken (4xx)",
    "SERVER_ERROR": "Server error (5xx)", "REDIRECTS": "Redirects", "NOINDEX": "noindex", "ROBOTS_BLOCKED": "Blocked by robots.txt",
    "CANONICAL_ELSEWHERE": "Canonical points elsewhere", "NOT_HTML": "Not HTML",
}
TONE = {"fix": "warn", "site": "bad", "discovery": "warn", "ok": "good", "info": "muted"}


def inspect_link(prop, url):
    if not prop:
        return ""
    return f"https://search.google.com/search-console/inspect?resource_id={quote(prop, safe='')}&id={quote(url, safe='')}"


KNOWN = NOT_INDEXED + ("INDEXED",)


def fixable(r):
    """A technical item worth the owner's time: a sitemap conflict, a missing sitemap entry, a redirect over a live
    post, or a block on a page that earned Bing clicks. Deliberate exclusions (noindexed, out of sitemaps) are not."""
    f = r["flags"] or ""
    if "IGNORED" in f:
        return False
    return ((r["bucket"] in BLOCKERS and (r["in_sitemap"] or "BING_EARNER" in f or "PUBLISHED_BUT_REDIRECTED" in f))
            or "NOT_IN_SITEMAP" in f)


def indexed_share(buckets):
    known = sum(buckets.get(k, 0) for k in KNOWN)
    return buckets.get("INDEXED", 0) / known if known >= 20 else None


def _csv(path, rows, fields):
    with open(path, "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.DictWriter(fh, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


def write(db, results):
    out = config.OUT
    out.mkdir(parents=True, exist_ok=True)
    rows = [dict(r) for r in db.execute("""SELECT t.*, u.kind, u.in_sitemap, a.words, a.title FROM triage t JOIN urls u ON u.url=t.url
                                           LEFT JOIN audits a ON a.url=t.url ORDER BY t.site, t.value DESC""")]
    fields = ["site", "url", "bucket", "flags", "reason", "action", "value", "google", "bing", "kind", "in_sitemap", "words", "title"]
    _csv(out / "urls.csv", rows, fields)
    _csv(out / "fix_technical.csv", [r for r in rows if fixable(r)], fields)
    _csv(out / "google_not_indexed.csv", [r for r in rows if r["bucket"] in NOT_INDEXED], fields)

    plan, manual, dups = [], [], []
    for site, res in results.items():
        prop = res["meta"].get("gsc_property")
        plan += [dict(p, site=site) for p in res["plan"]]
        manual += [dict(m, site=site, search_console_link=inspect_link(prop, m["url"])) for m in res["manual"]]
        seen = set()
        for u, (other, j) in res["dups"].items():
            key = tuple(sorted((u, other)))
            if key not in seen:
                seen.add(key)
                dups.append({"site": site, "url": key[0], "similar_to": key[1], "estimated_overlap": j})
    _csv(out / "internal_link_plan.csv", plan, ["site", "target", "target_title", "bucket", "source", "source_title",
                                                 "shared_terms", "similarity", "source_bing_clicks", "source_google_clicks"])
    _csv(out / "request_indexing_today.csv", manual, ["site", "url", "bucket", "value", "search_console_link"])
    _csv(out / "near_duplicates.csv", dups, ["site", "url", "similar_to", "estimated_overlap"])

    summary = []
    for site, res in results.items():
        c = res["counts"]
        base = None
        for snap in db.execute("SELECT data FROM snapshots WHERE site=? ORDER BY run_at", (site,)):
            base = indexed_share(json.loads(snap["data"])["buckets"])
            if base is not None:
                break
        now_share = indexed_share(c)
        summary.append({"site": site, "urls": sum(c.values()), "conflicts": res["snapshot"]["conflicts"],
                        "excluded": res["snapshot"]["excluded"],
                        "indexed": c.get("INDEXED", 0), "crawled_not_indexed": c.get("CRAWLED_NOT_INDEXED", 0),
                        "discovered_not_indexed": c.get("DISCOVERED_NOT_INDEXED", 0), "unknown": c.get("UNKNOWN_TO_GOOGLE", 0),
                        "stale_or_other": c.get("GOOGLE_STALE", 0) + c.get("GOOGLE_PICKED_OTHER", 0),
                        "not_inspected": c.get("NOT_INSPECTED", 0) + c.get("NOT_CHECKED", 0),
                        "bing_earners": res["snapshot"]["bing_earners"], "indexnow": res["meta"].get("indexnow", "not checked"),
                        "indexed_share": f"{now_share:.0%}" if now_share is not None else "",
                        "share_change": f"{(now_share - base) * 100:+.0f} pts" if now_share is not None and base is not None else ""})
    _csv(out / "summary.csv", summary, list(summary[0]) if summary else ["site"])
    (out / "report.html").write_text(render(results, summary, rows, plan), encoding="utf-8")
    return out / "report.html"


def _num(n, tone=""):
    return f'<td class="n {tone}">{n:,}</td>' if isinstance(n, int) else f"<td>{html.escape(str(n))}</td>"


def render(results, summary, rows, plan):
    e = html.escape
    tot = {k: sum(s[k] for s in summary) for k in ("urls", "conflicts", "excluded", "indexed", "crawled_not_indexed",
                                                     "discovered_not_indexed", "unknown", "stale_or_other", "not_inspected", "bing_earners")}
    known = tot["indexed"] + tot["crawled_not_indexed"] + tot["discovered_not_indexed"] + tot["unknown"] + tot["stale_or_other"]
    by_site = {}
    for r in rows:
        by_site.setdefault(r["site"], []).append(r)

    site_rows = "".join(
        f"<tr><th scope=row>{e(s['site'])}</th>{_num(s['urls'])}{_num(s['conflicts'], 'warn' if s['conflicts'] else '')}"
        f"{_num(s['excluded'], 'muted')}{_num(s['indexed'], 'good')}{_num(s['crawled_not_indexed'])}{_num(s['discovered_not_indexed'])}"
        f"{_num(s['unknown'])}{_num(s['stale_or_other'])}{_num(s['not_inspected'], 'muted')}{_num(s['bing_earners'])}"
        f"<td>{e(str(s['indexnow']))}</td><td class=n>{e(s['indexed_share'] or '-')}"
        f"{' <span class=sub>(' + e(s['share_change']) + ')</span>' if s['share_change'] else ''}</td></tr>" for s in summary)

    sections = []
    for site, res in results.items():
        prop = res["meta"].get("gsc_property")
        diag = "".join(f'<li class="{TONE.get(k, "muted")}">{e(m)}</li>' for k, m in res["diagnosis"]) or "<li class=muted>Nothing to flag.</li>"
        todo = [r for r in by_site.get(site, []) if r["bucket"] in NOT_INDEXED or fixable(r)][:15]
        todo_html = "".join(
            f"<tr><td><a href='{e(r['url'])}'>{e(r['title'] or r['url'])}</a><div class=sub>{e(r['url'])}</div></td>"
            f"<td><span class='tag {'warn' if r['bucket'] in BLOCKERS else ''}'>{e(LABEL.get(r['bucket'], r['bucket']))}</span>"
            f"<div class=sub>{'Bing: ' + e(r['bing']) if r['bing'] else ''}</div></td><td>{e(r['action'])}</td></tr>" for r in todo)
        manual = "".join(f"<li><a href='{e(inspect_link(prop, m['url']))}'>{e(m['url'])}</a> "
                         f"<span class=sub>{e(LABEL.get(m['bucket'], m['bucket']))}</span></li>" for m in res["manual"])
        links = [p for p in res["plan"]][:10]
        links_html = "".join(f"<li>On <a href='{e(p['source'])}'>{e(p['source_title'] or p['source'])}</a> link to "
                             f"<a href='{e(p['target'])}'>{e(p['target_title'] or p['target'])}</a> "
                             f"<span class=sub>shared: {e(p['shared_terms'])}</span></li>" for p in links)
        c = res["counts"]
        bar = "".join(f'<span class="seg {b}" style="flex:{n}" title="{e(LABEL.get(b, b))}: {n}"></span>'
                      for b, n in sorted(c.items(), key=lambda kv: kv[0]) if n)
        sections.append(f"""
<details class=site {'open' if len(results) == 1 else ''}>
<summary><h2>{e(site)}</h2><span class=sub>{sum(c.values()):,} URLs &middot; {c.get('INDEXED', 0):,} indexed by Google</span></summary>
<div class=bar>{bar}</div>
<h3>Diagnosis</h3><ul class=diag>{diag}</ul>
<h3>Highest-value pages to fix</h3>
{'<table class=todo><thead><tr><th>Page</th><th>State</th><th>What to do</th></tr></thead><tbody>' + todo_html + '</tbody></table>' if todo_html else '<p class=muted>None.</p>'}
<h3>Request indexing today <span class=sub>(Search Console button; only where a request can help)</span></h3>
{'<ol>' + manual + '</ol>' if manual else '<p class=muted>Nothing qualifies today.</p>'}
<h3>Internal links to add <span class=sub>(from pages Google already indexes or that earn Bing clicks)</span></h3>
{'<ul class=plan>' + links_html + '</ul><p class=sub>Full list: internal_link_plan.csv</p>' if links_html else '<p class=muted>No suggestions yet (needs Google data or Bing clicks to know which pages are trusted).</p>'}
</details>""")

    return f"""<!doctype html><html lang=en><head><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1">
<title>Index Engine Report</title>
<style>
:root{{--bg:#f7f7f5;--card:#fff;--fg:#1d1d1b;--muted:#6b6b66;--line:#e3e2dc;--good:#1f7a4d;--warn:#a15c00;--bad:#b42318;--accent:#2f5bd3}}
@media (prefers-color-scheme:dark){{:root:not([data-theme=light]){{--bg:#151514;--card:#1f1f1d;--fg:#ecebe6;--muted:#9a9a93;--line:#34342f;--good:#4cc38a;--warn:#e7a23b;--bad:#f0705f;--accent:#7f9cf5}}}}
:root[data-theme=dark]{{--bg:#151514;--card:#1f1f1d;--fg:#ecebe6;--muted:#9a9a93;--line:#34342f;--good:#4cc38a;--warn:#e7a23b;--bad:#f0705f;--accent:#7f9cf5}}
*{{box-sizing:border-box}}body{{margin:0;background:var(--bg);color:var(--fg);font:15px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif}}
main{{max-width:1180px;margin:0 auto;padding:24px 16px 64px}}h1{{font-size:24px;margin:0 0 4px}}h2{{font-size:18px;margin:0;display:inline}}
h3{{font-size:14px;text-transform:uppercase;letter-spacing:.04em;color:var(--muted);margin:22px 0 8px}}
a{{color:var(--accent)}}.sub{{color:var(--muted);font-size:13px}}.muted{{color:var(--muted)}}.good{{color:var(--good)}}.warn{{color:var(--warn)}}.bad{{color:var(--bad)}}
.kpis{{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px;margin:18px 0}}
.kpi{{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:12px 14px}}.kpi b{{display:block;font-size:24px;font-variant-numeric:tabular-nums}}
.wrap{{overflow-x:auto;background:var(--card);border:1px solid var(--line);border-radius:10px}}
table{{border-collapse:collapse;width:100%}}th,td{{padding:8px 10px;border-bottom:1px solid var(--line);text-align:left;vertical-align:top}}
thead th{{font-size:12px;color:var(--muted);font-weight:600}}td.n{{text-align:right;font-variant-numeric:tabular-nums}}
details.site{{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px 16px;margin:12px 0}}
summary{{cursor:pointer;display:flex;gap:12px;align-items:baseline;flex-wrap:wrap}}
.bar{{display:flex;height:10px;border-radius:5px;overflow:hidden;margin:12px 0;background:var(--line)}}.seg{{min-width:2px}}
.seg.INDEXED{{background:var(--good)}}.seg.CRAWLED_NOT_INDEXED{{background:var(--bad)}}.seg.DISCOVERED_NOT_INDEXED{{background:#d9822b}}
.seg.UNKNOWN_TO_GOOGLE{{background:#c9a227}}.seg.NOT_INSPECTED,.seg.NOT_CHECKED{{background:var(--muted)}}
.seg.GOOGLE_STALE,.seg.GOOGLE_PICKED_OTHER{{background:#8e6bd1}}
.seg.BROKEN,.seg.SERVER_ERROR,.seg.REDIRECTS,.seg.NOINDEX,.seg.ROBOTS_BLOCKED,.seg.CANONICAL_ELSEWHERE,.seg.FETCH_FAILED,.seg.NOT_HTML{{background:#5b5b55}}
.tag{{display:inline-block;font-size:12px;border:1px solid var(--line);border-radius:999px;padding:1px 8px;white-space:nowrap}}.tag.warn{{color:var(--warn)}}
table.todo td:first-child{{max-width:340px;word-break:break-word}}ul.diag li{{margin:6px 0}}ol li,ul.plan li{{margin:4px 0;word-break:break-word}}
.note{{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px 16px;margin-top:24px}}
</style></head><body><main>
<h1>Index Engine</h1><div class=sub>Generated {dt.datetime.now():%Y-%m-%d %H:%M} &middot; files in {e(str(config.OUT))}</div>
<div class=kpis>
<div class=kpi><b>{tot['urls']:,}</b>URLs your sites list</div>
<div class="kpi"><b class=good>{tot['indexed']:,}</b>indexed by Google{f" ({tot['indexed'] / known:.0%} of checked)" if known else ""}</div>
<div class=kpi><b class=bad>{tot['crawled_not_indexed']:,}</b>crawled, not indexed</div>
<div class=kpi><b>{tot['discovered_not_indexed'] + tot['unknown']:,}</b>discovered or unknown</div>
<div class=kpi><b class=warn>{tot['conflicts']:,}</b>in a sitemap but blocked</div>
<div class=kpi><b>{tot['excluded']:,}</b>published, kept out on purpose</div>
<div class=kpi><b>{tot['bing_earners']:,}</b>pages earning Bing clicks</div>
</div>
<div class=wrap><table><thead><tr><th>Site</th><th>URLs</th><th>In sitemap, blocked</th><th>Kept out</th><th>Indexed</th>
<th>Crawled, not idx</th><th>Discovered</th><th>Unknown</th><th>Stale / other</th><th>No data yet</th><th>Bing earners</th><th>IndexNow</th>
<th>Indexed share of checked (change)</th></tr></thead>
<tbody>{site_rows}</tbody></table></div>
{''.join(sections)}
<div class=note><b>How to read this.</b> Google's state per URL comes from the URL Inspection API (2,000 checks/day per property) or
from Search impressions in the last {config.EVIDENCE_DAYS} days. "Crawled, not indexed" and "Discovered, not crawled" are Google
choosing not to index. Re-submitting does not change that: the page, its links, or the site has to change. The technical buckets
(listed in a sitemap but noindexed, redirected, broken or canonical elsewhere) are the only ones a fix is guaranteed to clear.
"Kept out" are published posts that are noindexed or redirected and also left out of the sitemaps: a deliberate exclusion, not a fault.
Google states marked [Google checked YYYY-MM-DD] are as of that date.
Work lists: fix_technical.csv, google_not_indexed.csv, internal_link_plan.csv, request_indexing_today.csv, near_duplicates.csv, urls.csv.</div>
</main></body></html>"""
