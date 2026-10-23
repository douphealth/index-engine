# -*- coding: utf-8 -*-
"""Offline end-to-end test: a fake WordPress-like site on localhost plus fake Google, Bing and
IndexNow clients. Exercises every stage and checks each decision. No network beyond 127.0.0.1."""
import json
import random
import shutil
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from . import audit, bing, config, discover, google, report, store, submit, triage, util

KEY = "abcdef1234567890abcdef1234567890"
VOCAB = [f"w{i}x" for i in range(600)]
LASTMOD = "2026-10-20T10:00:00+00:00"
PAGES = {   # path: (title, words, extra)
    "/good-indexed/": ("Ceramic Planter Drainage Guide", 900, {}),
    "/good-crawled/": ("Calathea Ceramic Planter Drainage", 700, {}),
    "/good-discovered/": ("Ceramic Planter Drainage Mistakes", 800, {}),
    "/good-unknown/": ("Monstera Aerial Roots Explained", 750, {}),
    "/noindex/": ("Thank You", 400, {"noindex": True}),
    "/canon-other/": ("Drainage Guide Print Version", 900, {"canonical": "/good-indexed/"}),
    "/private/secret/": ("Private Notes", 500, {}),
    "/thin/": ("Pothos Quick Note", 60, {}),
    "/dup-a/": ("Snake Plant Watering Schedule", 600, {"dup": True}),
    "/dup-b/": ("How Often To Water Snake Plants", 600, {"dup": True}),
    "/orphan/": ("Fiddle Leaf Fig Leaf Drop", 650, {}),
    "/not-in-sitemap/": ("Peace Lily Brown Tips", 700, {}),
    "/about/": ("About Us", 500, {"hub": True}),
}


def body_text(path, n, dup=False):
    rnd = random.Random("dup" if dup else path)
    return " ".join(rnd.choice(VOCAB) for _ in range(n))


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="text/html; charset=utf-8", headers=None):
        data = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        base = f"http://{self.headers['Host']}"
        u = urlsplit(self.path)
        p = u.path
        if p == "/robots.txt":
            return self._send(200, f"User-agent: *\nDisallow: /private/\nSitemap: {base}/sitemap_index.xml\n"
                                   f"# IndexNow key file: {base}/{KEY}.txt\n", "text/plain")
        if p == f"/{KEY}.txt":
            return self._send(200, KEY, "text/plain")
        if p == "/sitemap_index.xml":
            items = "".join(f"<sitemap><loc>{base}/{n}</loc></sitemap>" for n in ("post-sitemap.xml", "page-sitemap.xml", "category-sitemap.xml"))
            return self._send(200, f'<?xml version="1.0"?><sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">{items}</sitemapindex>', "application/xml")
        if p in ("/post-sitemap.xml", "/page-sitemap.xml", "/category-sitemap.xml"):
            if p == "/post-sitemap.xml":
                paths = [x for x in PAGES if x not in ("/about/", "/not-in-sitemap/")] + ["/redirect-me/", "/gone/"]
            elif p == "/page-sitemap.xml":
                paths = ["/about/"]
            else:
                paths = ["/category/plants/"]
            items = "".join(f"<url><loc>{base}{x}</loc><lastmod>{LASTMOD}</lastmod>"
                            f"<image:image><image:loc>{base}/img{i}.jpg</image:loc></image:image></url>" for i, x in enumerate(paths))
            return self._send(200, '<?xml version="1.0"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9" '
                                   f'xmlns:image="http://www.google.com/schemas/sitemap-image/1.1">{items}</urlset>', "application/xml")
        if p == "/wp-json/wp/v2/posts":
            data = [{"link": f"{base}{x}", "modified_gmt": "2026-10-20T10:00:00"} for x in ("/good-indexed/", "/not-in-sitemap/")]
            return self._send(200, json.dumps(data), "application/json", {"X-WP-TotalPages": "1"})
        if p == "/wp-json/wp/v2/pages":
            return self._send(200, "[]", "application/json", {"X-WP-TotalPages": "1"})
        if p == "/redirect-me/":
            self.send_response(301)
            self.send_header("Location", f"{base}/good-indexed/")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if p == "/category/plants/":
            return self._send(200, self.page(base, p, "Plants", 300, {}))
        if p not in PAGES:
            return self._send(404, "<html><head><title>Not found</title></head><body>404</body></html>")
        title, n, extra = PAGES[p]
        return self._send(200, self.page(base, p, title, n, extra))

    def page(self, base, path, title, n, extra):
        head = f"<title>{title} | Test Plants</title>"
        head += f'<link rel="canonical" href="{base}{extra.get("canonical", path)}">'
        if extra.get("noindex"):
            head += '<meta name="robots" content="noindex, follow">'
        nav = f'<nav><a href="{base}/about/">About</a> <a href="{base}/good-unknown/">Monstera</a></nav>'
        links = ""
        if extra.get("hub"):
            links = " ".join(f'<a href="{base}{x}">{t}</a>' for x, (t, _, _) in PAGES.items()
                             if x not in ("/orphan/", "/good-unknown/", "/about/"))
        if path == "/good-indexed/":
            links = f'<a href="{base}/good-crawled/">calathea</a>'
        text = body_text(path, n, extra.get("dup"))
        return (f"<!doctype html><html><head>{head}</head><body><header>{nav}</header><main><article>"
                f"<h1>{title}</h1><div class='entry-content'><p>{text}</p><p>{links}</p></div></article></main>"
                f"<aside>sidebar words here</aside><footer>footer</footer></body></html>")


class FakeGSC:
    COVERAGE = {"/good-crawled/": "Crawled - currently not indexed", "/good-discovered/": "Discovered - currently not indexed",
                "/good-unknown/": "URL is unknown to Google", "/dup-a/": "Duplicate, Google chose different canonical than user",
                "/dup-b/": "Crawled - currently not indexed", "/about/": "Submitted and indexed",
                "/orphan/": "Discovered - currently not indexed", "/not-in-sitemap/": "URL is unknown to Google",
                "/thin/": "Crawled - currently not indexed"}

    def __init__(self, base):
        self.base, self.inspected, self.submitted = base, [], []

    def list_sites(self):
        return [{"siteUrl": f"sc-domain:{urlsplit(self.base).netloc}", "permissionLevel": "siteOwner"}]

    def search_pages(self, prop, start, end):
        return [{"keys": [self.base + "/good-indexed/"], "clicks": 3, "impressions": 120}]

    def inspect(self, prop, url):
        self.inspected.append(urlsplit(url).path)
        cov = self.COVERAGE.get(urlsplit(url).path, "URL is unknown to Google")
        return {"verdict": "PASS" if "and indexed" in cov else "NEUTRAL", "coverage": cov,
                "last_crawl": "2026-10-01T00:00:00Z" if "Crawled" in cov else None}

    def list_sitemaps(self, prop):
        return []

    def submit_sitemap(self, prop, feed):
        self.submitted.append(feed)
        return 200


class FakeBing:
    def __init__(self, base):
        self.base, self.sent = base, []

    def user_sites(self):
        return [{"Url": self.base + "/", "IsVerified": True}]

    def quota(self, site_url):
        return 100, 1000

    def submit(self, site_url, urls):
        self.sent += urls
        return 200, "{}"

    def page_stats(self, site_url):
        return [{"Query": self.base + "/dup-b/", "Clicks": 7, "Impressions": 100, "Date": "/Date(1760000000000)/"}]

    def url_info(self, site_url, url):
        return {"LastCrawledDate": "/Date(1760000000000)/" if url.endswith("/about/") else "/Date(-62135596800000)/",
                "DiscoveryDate": "/Date(1750000000000)/", "HttpStatus": 200}


class FakeIndexNow:
    def __init__(self):
        self.calls = []

    def submit(self, host, key, loc, urls):
        self.calls.append((host, key, loc, list(urls)))
        return 200, ""


def main():
    util.utf8_stdout()
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    tmp = Path(tempfile.mkdtemp(prefix="index-engine-selftest-"))
    config.OUT, config.DB_PATH, config.LOG_DIR = tmp, tmp / "t.db", tmp / "logs"
    config.INDEXNOW_KEYS_FILE = tmp / "none.json"
    util.open_log("selftest")
    fails, checks = [], []

    def check(cond, msg):
        print(("  ok   " if cond else "  FAIL ") + msg)
        checks.append(msg)
        if not cond:
            fails.append(msg)

    db = store.connect()
    sites = [base]
    discover.run(db, sites)
    urls = {urlsplit(r["url"]).path: dict(r) for r in db.execute("SELECT * FROM urls")}
    check(not any(p.endswith(".jpg") for p in urls), "image:loc entries are not counted as pages")
    check(urls["/not-in-sitemap/"]["in_wp"] == 1 and urls["/not-in-sitemap/"]["in_sitemap"] == 0, "published post missing from sitemaps is found via WP REST")
    check(urls["/category/plants/"]["kind"] == "taxonomy" and urls["/about/"]["kind"] == "page", "sitemap kinds detected")

    audit.run(db, sites)
    a = {urlsplit(r["url"]).path: dict(r) for r in db.execute("SELECT * FROM audits")}
    check(a["/redirect-me/"]["hops"] == 1, "redirect detected")
    check(a["/gone/"]["status"] == 404, "404 detected")
    check(a["/noindex/"]["noindex"] == 1, "meta noindex detected")
    check(a["/canon-other/"]["canonical_state"] == "other", "canonical to another URL detected")
    check(a["/private/secret/"]["robots_google"] == 0, "robots.txt disallow detected")
    check(a["/good-indexed/"]["words"] >= 900 and a["/good-indexed/"]["words"] < 960, "main-content words exclude nav/aside/footer")
    check(a["/good-indexed/"]["h1"] == "Ceramic Planter Drainage Guide", "h1 extracted")

    gsc, bng, inow = FakeGSC(base), FakeBing(base), FakeIndexNow()
    props = google.resolve_properties(db, gsc, sites)
    check(bool(props), "Search Console property resolved")
    google.pull_performance(db, gsc, props)
    bsites = bing.resolve_sites(db, bng, sites)
    bing.pull_performance(db, bng, bsites)
    google.inspect_due(db, gsc, props)
    check("/good-indexed/" not in gsc.inspected, "URL with recent impressions is not inspected (quota saved)")
    check(not {"/gone/", "/redirect-me/", "/noindex/", "/canon-other/", "/private/secret/", "/category/plants/"} & set(gsc.inspected),
          "technically blocked URLs and taxonomy archives are not inspected")
    n_inspected = len(gsc.inspected)
    google.inspect_due(db, gsc, props)
    check(len(gsc.inspected) == n_inspected, "fresh inspections are not repeated")
    used, _ = store.quota_used(db, util.pacific_day(), props[base])
    check(used == n_inspected, "inspection quota is tracked")

    submit.run(db, sites, bing_client=bng, bing_sites=bsites, indexnow_client=inow)
    sent = {urlsplit(u).path for c in inow.calls for u in c[3]}
    check(sent and not ({"/gone/", "/redirect-me/", "/noindex/", "/canon-other/", "/private/secret/", "/category/plants/"} & sent),
          "only indexable URLs are submitted")
    check("/good-indexed/" in sent and "/not-in-sitemap/" in sent, "indexable URLs (including one missing from the sitemap) are submitted")
    check(not bng.sent, "Bing API not used when IndexNow works (no duplicate pings)")
    n_calls = len(inow.calls)
    submit.run(db, sites, bing_client=bng, bing_sites=bsites, indexnow_client=inow)
    check(len(inow.calls) == n_calls, "unchanged URLs are not resubmitted")
    db.execute("UPDATE urls SET lastmod='2026-10-25T00:00:00+00:00' WHERE url LIKE '%/good-crawled/'")
    submit.run(db, sites, bing_client=bng, bing_sites=bsites, indexnow_client=inow)
    check(len(inow.calls) == n_calls + 1 and [urlsplit(u).path for u in inow.calls[-1][3]] == ["/good-crawled/"],
          "a changed URL is resubmitted alone")
    submit.run(db, sites, bing_client=bng, bing_sites=bsites, indexnow_client=inow, both=True)
    check(len(bng.sent) > 0, "--both also uses the Bing API")

    # A site with its own pre-submission gate: only PASS lines go out; a gate that crashes sends nothing.
    gate_dir = tmp / "site" / "site_guard"
    gate_dir.mkdir(parents=True)
    (gate_dir / "gate_ok.py").write_text(
        "import sys\nf = sys.argv[sys.argv.index('--file') + 1]\n"
        "for u in open(f, encoding='utf-8').read().split():\n"
        "    print(('BLOCK ' + u + '  <- noindex') if '/thin/' in u else ('PASS  ' + u))\n", encoding="utf-8")
    (gate_dir / "gate_crash.py").write_text("raise SystemExit('baseline missing')\n", encoding="utf-8")
    db.execute("UPDATE urls SET lastmod='2026-10-26T00:00:00+00:00' WHERE url LIKE '%/thin/' OR url LIKE '%/good-unknown/'")
    n_calls = len(inow.calls)
    config.SITE_GATES[base] = gate_dir / "gate_crash.py"
    submit.run(db, sites, bing_client=bng, bing_sites=bsites, indexnow_client=inow)
    check(len(inow.calls) == n_calls, "a site gate that fails to give verdicts blocks every submission")
    config.SITE_GATES[base] = gate_dir / "gate_ok.py"
    submit.run(db, sites, bing_client=bng, bing_sites=bsites, indexnow_client=inow)
    check(len(inow.calls) == n_calls + 1 and [urlsplit(u).path for u in inow.calls[-1][3]] == ["/good-unknown/"],
          "only URLs the site gate passes are submitted")
    del config.SITE_GATES[base]
    google.sync_sitemaps(db, gsc, props)
    check(gsc.submitted == [f"{base}/sitemap_index.xml"], "unregistered sitemap submitted to Search Console once")
    google.sync_sitemaps(db, gsc, props)
    check(len(gsc.submitted) == 1, "sitemap is not resubmitted without new URLs")
    bing.check_url_info(db, bng, bsites)
    check(db.execute("SELECT COUNT(*) FROM bing_info").fetchone()[0] == 0, "no Bing crawl checks on the day of submission")
    db.execute("UPDATE submissions SET submitted_at='2026-10-01T00:00:00+00:00'")
    bing.check_url_info(db, bng, bsites)
    check(db.execute("SELECT COUNT(*) FROM bing_info WHERE last_crawled IS NOT NULL").fetchone()[0] == 1, "Bing crawl status recorded")
    check(db.execute("SELECT COUNT(*) FROM bing_info WHERE url LIKE '%/dup-b/'").fetchone()[0] == 0,
          "pages Bing already shows are not crawl-checked")

    res = triage.run(db, sites)
    t = {urlsplit(r["url"]).path: dict(r) for r in db.execute("SELECT * FROM triage")}
    expect = {"/good-indexed/": "INDEXED", "/about/": "INDEXED", "/good-crawled/": "CRAWLED_NOT_INDEXED",
              "/good-discovered/": "DISCOVERED_NOT_INDEXED", "/good-unknown/": "UNKNOWN_TO_GOOGLE", "/noindex/": "NOINDEX",
              "/redirect-me/": "REDIRECTS", "/gone/": "BROKEN", "/canon-other/": "CANONICAL_ELSEWHERE",
              "/private/secret/": "ROBOTS_BLOCKED", "/dup-a/": "GOOGLE_PICKED_OTHER"}
    for path, bucket in expect.items():
        check(t[path]["bucket"] == bucket, f"{path} -> {bucket} (got {t[path]['bucket']})")
    check("THIN" in t["/thin/"]["flags"], "thin page flagged")
    check("NEAR_DUPLICATE" in t["/dup-a/"]["flags"] and "NEAR_DUPLICATE" in t["/dup-b/"]["flags"], "near-duplicates found")
    check("BING_EARNER" in t["/dup-b/"]["flags"] and "KEEP LIVE" in t["/dup-b/"]["action"], "Bing earner protected from removal")
    check("ORPHAN" in t["/orphan/"]["flags"], "orphan page flagged")
    check("NO_BODY_LINKS" in t["/good-unknown/"]["flags"], "menu-only page flagged")
    check("NOT_IN_SITEMAP" in t["/not-in-sitemap/"]["flags"], "missing-from-sitemap flagged")
    r = res[base]
    manual = {urlsplit(m["url"]).path for m in r["manual"]}
    check("/good-crawled/" in manual, "crawled-not-indexed page that changed since the crawl is queued for a manual request")
    check("/thin/" not in manual and "/dup-b/" not in manual, "thin and near-duplicate pages are not queued")
    plan = {(urlsplit(p["source"]).path, urlsplit(p["target"]).path) for p in r["plan"]}
    check(("/good-indexed/", "/good-discovered/") in plan, "link plan: indexed related page suggested as source")
    check(("/good-indexed/", "/good-crawled/") not in plan, "link plan skips links that already exist")
    # Owner decisions pulled from the dashboard
    from . import dashboard, publish
    dashboard.apply_state(db, {"settings": {}, "url_state": [
        {"url": base + "/good-unknown/", "site": base, "ignored": 1, "requested_at": None},
        {"url": base + "/good-crawled/", "site": base, "ignored": 0, "requested_at": util.now_iso()}]})
    res2 = triage.run(db, sites)
    manual2 = {urlsplit(m["url"]).path for m in res2[base]["manual"]}
    check("/good-unknown/" not in manual2 and "/good-crawled/" not in manual2,
          "ignored and already-requested URLs leave the request-indexing list")
    t2 = {urlsplit(r["url"]).path: dict(r) for r in db.execute("SELECT * FROM triage")}
    check("IGNORED" in t2["/good-unknown/"]["flags"] and t2["/good-unknown/"]["bucket"] == "UNKNOWN_TO_GOOGLE",
          "an ignored URL keeps its true state but is flagged IGNORED")
    db.execute("UPDATE urls SET lastmod='2026-10-28T00:00:00+00:00' WHERE url LIKE '%/good-unknown/' OR url LIKE '%/about/'")
    n_calls = len(inow.calls)
    submit.run(db, sites, bing_client=bng, bing_sites=bsites, indexnow_client=inow)
    sent2 = [urlsplit(u).path for u in inow.calls[-1][3]] if len(inow.calls) > n_calls else []
    check("/about/" in sent2 and "/good-unknown/" not in sent2, "an ignored URL is never submitted, even when it changes")
    summary, tables, run_sum = publish.build(db, res2, util.now_iso(), {base: {"indexnow": 1}})
    check(summary["sites"][0]["site"] == base and tables[base]["cols"][0] == "url" and len(tables[base]["rows"]) == len(t2)
          and run_sum["totals"]["submitted"] == 1 and run_sum["totals"]["known"] > 0, "dashboard payload built")
    res = res2

    path = report.write(db, res)
    html = path.read_text(encoding="utf-8")
    check(base in html and "Diagnosis" in html, "HTML report written")
    logs = "".join(p.read_text(encoding="utf-8") for p in (tmp / "logs").glob("*.txt"))
    check(KEY not in logs and KEY not in html, "IndexNow key never appears in logs or report")

    server.shutdown()
    db.close()
    util.close_log()
    if fails:
        print(f"\n{len(fails)} of {len(checks)} checks FAILED - test files kept in {tmp}")
        return 1
    shutil.rmtree(tmp, ignore_errors=True)
    print(f"\nALL {len(checks)} CHECKS PASSED")
    return 0
