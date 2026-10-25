# -*- coding: utf-8 -*-
"""Offline end-to-end test: a fake WordPress-like site on localhost plus fake Google, Bing and
IndexNow clients. Exercises every stage and checks each decision. No network beyond 127.0.0.1."""
import base64
import json
import random
import shutil
import tempfile
import threading
from collections import defaultdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import numpy as np

from . import audit, bing, brain, config, discover, fresh, google, links, model, report, store, submit, triage, util, wp

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
    "/brand-new/": ("Watering Brand New Succulents", 500, {}),
}
UNLISTED = ("/about/", "/not-in-sitemap/", "/brand-new/")   # not in the post sitemap
RAW = {}            # path -> stored post content (fake WordPress); rendered instead of generated text
FLIP = set()        # paths that currently serve noindex (regression test)
FRESH = []          # (path, modified_gmt) returned by modified_after queries
WP_USER, WP_PW = "tester", "abcd efgh ijkl mnop qrst uvwx"
POST_IDS = {p: 100 + i for i, p in enumerate(PAGES)}
EDGE = {}           # path -> html an edge cache (Cloudflare) keeps serving on the plain URL


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
                paths = [x for x in PAGES if x not in UNLISTED] + ["/redirect-me/", "/gone/"]
            elif p == "/page-sitemap.xml":
                paths = ["/about/"]
            else:
                paths = ["/category/plants/"]
            items = "".join(f"<url><loc>{base}{x}</loc><lastmod>{LASTMOD}</lastmod>"
                            f"<image:image><image:loc>{base}/img{i}.jpg</image:loc></image:image></url>" for i, x in enumerate(paths))
            return self._send(200, '<?xml version="1.0"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9" '
                                   f'xmlns:image="http://www.google.com/schemas/sitemap-image/1.1">{items}</urlset>', "application/xml")
        if p.startswith("/wp-json/wp/v2/"):
            return self.wp_get(base, p, parse_qs(u.query))
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
        if p in EDGE and not u.query:
            return self._send(200, EDGE[p], headers={"cf-cache-status": "HIT", "Age": "600"})
        title, n, extra = PAGES[p]
        return self._send(200, self.page(base, p, title, n, extra), headers={"cf-cache-status": "MISS"})

    # ---- fake WordPress REST API: login, slug lookup, raw content, save, modified_after
    def _authed(self):
        tok = base64.b64encode(f"{WP_USER}:{WP_PW}".encode()).decode()
        return self.headers.get("Authorization") == f"Basic {tok}"

    def wp_get(self, base, p, q):
        js = lambda data, code=200: self._send(code, json.dumps(data), "application/json", {"X-WP-TotalPages": "1"})  # noqa: E731
        if p == "/wp-json/wp/v2/users/me":
            return js({"id": 1}) if self._authed() else js({"code": "rest_not_logged_in"}, 401)
        if p == "/wp-json/wp/v2/pages":
            return js([])
        if p == "/wp-json/wp/v2/posts":
            if "slug" in q:
                path = f"/{q['slug'][0]}/"
                return js([{"id": POST_IDS[path], "link": f"{base}{path}", "status": "publish"}] if path in POST_IDS else [])
            if q.get("context") == ["edit"]:                 # bulk content for the link finder
                if not self._authed():
                    return js({"code": "rest_forbidden_context"}, 401)
                after = (q.get("modified_after") or [""])[0]
                paths = [x for x, m in FRESH if m > after] if after else list(PAGES)
                return js([{"id": POST_IDS[x], "link": f"{base}{x}", "modified_gmt": "2026-10-20T10:00:00",
                            "content": {"raw": RAW.get(x) or f"<p>{body_text(x, PAGES[x][1], PAGES[x][2].get('dup'))}</p>"}}
                           for x in paths if x in POST_IDS])
            if "modified_after" in q:
                return js([{"link": f"{base}{x}", "modified_gmt": m, "date_gmt": m} for x, m in FRESH])
            return js([{"link": f"{base}{x}", "modified_gmt": "2026-10-20T10:00:00", "date_gmt": "2026-06-01T10:00:00"}
                       for x in ("/good-indexed/", "/not-in-sitemap/")])
        pid = int(p.rsplit("/", 1)[-1]) if p.rsplit("/", 1)[-1].isdigit() else None
        path = next((k for k, v in POST_IDS.items() if v == pid), None)
        if path and self._authed():
            return js({"id": pid, "link": f"{base}{path}", "modified_gmt": "2026-10-20T10:00:00", "content": {"raw": RAW.get(path, "")}})
        return js({"code": "rest_forbidden"}, 401)

    def do_POST(self):
        p = urlsplit(self.path).path
        pid = int(p.rsplit("/", 1)[-1]) if p.rsplit("/", 1)[-1].isdigit() else None
        path = next((k for k, v in POST_IDS.items() if v == pid), None)
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        if not (path and self._authed()):
            return self._send(401, "{}", "application/json")
        RAW[path] = body.get("content", RAW.get(path, ""))
        return self._send(200, json.dumps({"id": pid}), "application/json")

    def page(self, base, path, title, n, extra):
        head = f"<title>{title} | Test Plants</title>"
        head += f'<link rel="canonical" href="{base}{extra.get("canonical", path)}">'
        if extra.get("noindex") or path in FLIP:
            head += '<meta name="robots" content="noindex, follow">'
        nav = f'<nav><a href="{base}/about/">About</a> <a href="{base}/good-unknown/">Monstera</a></nav>'
        links = ""
        if extra.get("hub"):
            links = " ".join(f'<a href="{base}{x}">{t}</a>' for x, (t, _, _) in PAGES.items()
                             if x not in ("/orphan/", "/good-unknown/", "/about/", "/brand-new/"))
        text = body_text(path, n, extra.get("dup"))
        if path == "/good-crawled/":
            text += " In my practice my clients often ask how often to water."
        inner = RAW[path] if path in RAW else f"<p>{text}</p><p>{links}</p>"
        return (f"<!doctype html><html><head>{head}</head><body><header>{nav}</header><main><article>"
                f"<h1>{title}</h1><div class='entry-content'>{inner}</div></article></main>"
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
    RAW["/good-indexed/"] = (f"<p>{body_text('/good-indexed/', 900)}</p><h2>Ceramic Planter Drainage Mistakes</h2>"
                             "<p>Many beginners make ceramic planter drainage mistakes when potting.</p>"
                             f'<p><a href="{base}/good-crawled/">calathea</a></p>')
    tmp = Path(tempfile.mkdtemp(prefix="index-engine-selftest-"))
    config.OUT, config.DB_PATH, config.LOG_DIR = tmp, tmp / "t.db", tmp / "logs"
    config.INDEXNOW_KEYS_FILE = tmp / "none.json"
    config.CF_TOKEN_FILE = tmp / "no-token.txt"
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

    # A failed inspection must not destroy previously verified Google evidence.
    old = db.execute("SELECT coverage, verdict FROM gsc WHERE url = ?", (base + "/good-crawled/",)).fetchone()
    original_inspect = gsc.inspect
    gsc.inspect = lambda prop, url: {"error": "HTTP 503: temporary outage", "http": 503}
    db.execute("UPDATE gsc SET inspected_at='2026-01-01T00:00:00+00:00' WHERE url=?", (base + "/good-crawled/",))
    google.inspect_due(db, gsc, props, only_urls=[base + "/good-crawled/"])
    after = db.execute("SELECT coverage, verdict, error FROM gsc WHERE url=?", (base + "/good-crawled/",)).fetchone()
    check(after["coverage"] == old["coverage"] and after["verdict"] == old["verdict"] and bool(after["error"]),
          "transient GSC errors preserve the last known indexing verdict")
    gsc.inspect = original_inspect
    db.execute("UPDATE gsc SET error=NULL, inspected_at='2026-10-25T00:00:00+00:00' WHERE url=?",
               (base + "/good-crawled/",))


    submit.run(db, sites, bing_client=bng, bing_sites=bsites, indexnow_client=inow, max_per_site=0)
    check(not inow.calls, "zero submission limit sends nothing")
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

    # ---- intelligence: trust scan, odds model, best actions, impact
    t3 = {urlsplit(r["url"]).path: dict(r) for r in db.execute("SELECT * FROM triage")}
    check("TRUST_RISK" in t3["/good-crawled/"]["flags"], "invented first-hand claims are flagged (trust scan)")
    rng = np.random.default_rng(7)
    X = rng.normal(size=(400, len(model.NAMES)))
    y = ((X[:, 1] + 0.5 * X[:, 0] + rng.normal(scale=0.6, size=400)) > 0).astype(float)
    check(model.auc(y, model.predict(model.fit(X, y), X)) > 0.85, "odds model learns a real signal (synthetic AUC > 0.85)")
    check(model.auc([1, 1, 0, 0], [0.9, 0.8, 0.2, 0.1]) == 1.0 and model.auc([1, 0], [0.5, 0.5]) == 0.5,
          "AUC is computed correctly (ties count half)")
    res3, intel = brain.analyze(db, sites)
    check(intel["model"]["quality"] == "insufficient" and intel["actions"],
          "with too few Google verdicts the model says so and falls back to fixed estimates")
    check(db.execute("SELECT COUNT(*) FROM triage WHERE odds IS NOT NULL").fetchone()[0] > 0
          and any(a["action"] == "add_links" and a["url"].endswith("/good-discovered/") for a in intel["actions"]),
          "scored pages get odds; best actions include links for a stuck page")
    check({"gained", "lost", "requests", "links", "bing"} <= set(intel["impact"]), "impact report built")
    s3 = publish.build(db, res3, util.now_iso(), {}, (), intel)[0]["sites"][0]
    pot = s3["potential"].get("add_links") or {}
    check(pot.get("n", 0) >= 1 and 0 < pot.get("pages", 0) <= pot["n"] and isinstance(pot["pages"], float)
          and s3["flags"].get("ORPHAN", 0) >= 1 and "IGNORED" not in s3["flags"],
          "the summary carries what each kind of work could gain (all pages, not just the top) and flag counts for the priorities")

    # ---- link autopilot against the fake WordPress
    tricky = ('<h2>Ceramic planter drainage mistakes</h2>[caption]ceramic planter drainage mistakes[/caption]'
              '<p>Avoid <a href="/x/">ceramic planter drainage mistakes</a> and common ceramic planter drainage mistakes when potting.</p>')
    hit = links.find(tricky, "ceramic planter drainage mistakes", base + "/good-discovered/")
    check(bool(hit) and tricky[hit[0] - 7:hit[0]] == "common ", "anchor search skips headings, shortcodes and existing links")
    wpc = wp.WPClient(base, base=base, creds=[("nobody", "wrong pass"), (WP_USER, WP_PW)])
    check(wpc.ok, "WordPress login with an application password")
    props_ = links.propose(db, res3, intel["link_gain"], {base: wpc})
    prop = next((p for p in props_ if p["target"].endswith("/good-discovered/")), None)
    check(bool(prop) and prop["source"].endswith("/good-indexed/") and prop["anchor"].lower() == "ceramic planter drainage mistakes",
          "a link proposal uses a phrase already in the source page's text")
    check([t for t, _, _ in links.tokens("Women&#8217;s Running Shoes")] == ["women", "running", "shoe"]
          and links.stem("berries") == "berry" and links.stem("glass") == "glass",
          "anchor matching ignores plurals, possessives and HTML entities")
    keys = {("ceramic", "planter", "drainage", "mistake"): 1, ("drainage", "mistake"): 1}
    found = links.candidates("<p>Water pooled because of poor drainage. Mistakes like this one happen to everyone.</p>"
                             "<p>Watch for drainage <em>mistakes</em> that many new owners make.</p>"
                             "<p>Spider Plant Drainage Mistakes</p>"
                             "<p>Many beginners avoid Ceramic Planter Drainage Mistakes when potting.</p>", keys)
    check(found.get(("ceramic", "planter", "drainage", "mistake")) == (219, 252) and found.get(("drainage", "mistake")) == (235, 252),
          "anchors are found as plurals inside sentences, never across sentences or tags, never as a bare label")
    check(links.refresh_corpus(db, base, wpc) == 0 and len(links.corpus(db, base)) == len(PAGES),
          "post content is kept locally and re-read only when it changes")
    by_target = defaultdict(int)
    for p in props_:
        by_target[p["target"]] += 1
    check(max(by_target.values(), default=0) <= links.MAX_PER_TARGET
          and all(p["source"] != p["target"] and not p["anchor"].lower().startswith("calathea") for p in props_),
          "at most 3 new links per page, never a self-link, and only phrases that name the target alone")
    reports = []

    def rep(lid, st, result, at):
        reports.append((lid, st, result))

    lk = dict(prop or {}, status="approved")
    links.apply(db, [lk], {"links_per_site_per_day": 1}, {base: wpc}, rep)
    new_raw = RAW["/good-indexed/"]
    check(bool(reports) and reports[-1][1] == "applied" and new_raw.count(f'href="{lk.get("target")}"') == 1
          and "<h2>Ceramic Planter Drainage Mistakes</h2>" in new_raw and "visible to visitors" in reports[-1][2],
          "an approved link is applied once, inside the paragraph (heading untouched), and seen on the live page")
    n_rep = len(reports)
    other = dict(lk, id="0123456789abcdef", target=base + "/orphan/", anchor="beginners")
    links.apply(db, [other], {"links_per_site_per_day": 1}, {base: wpc}, rep)
    check(len(reports) == n_rep, "the daily per-site link cap is respected")
    links.STRICT_SITES.add(base)
    links.apply(db, [dict(other, status="proposed")], {"links_per_site_per_day": 5, "link_autopilot": True}, {base: wpc}, rep)
    links.STRICT_SITES.discard(base)
    check(len(reports) == n_rep, "autopilot never applies an unapproved link on a strict site")
    links.apply(db, [dict(lk, status="undo")], {}, {base: wpc}, rep)
    check(reports[-1][1] == "undone" and f'href="{lk.get("target")}"' not in RAW["/good-indexed/"],
          "undo removes exactly the added link")

    # ---- an edge cache hides the change: purge with the app token only, say so honestly, re-check until visible
    class FakeCF:
        def __init__(self):
            self.calls = []

        def _resp(self, data):
            return type("R", (), {"json": lambda _self: data})()

        def get(self, url, headers=None, params=None, timeout=None):
            self.calls.append(("GET", url, headers))
            return self._resp({"result": [{"id": "zone1"}]})

        def post(self, url, headers=None, json=None, timeout=None):
            self.calls.append(("POST", url, json))
            return self._resp({"success": False, "errors": [{"code": 10000, "message": "Authentication error"}]})

    EDGE["/good-indexed/"] = util.get(base + "/good-indexed/").text
    (tmp / "cf-token.txt").write_text("Index Engine token: selftest-cf-token-123", encoding="utf-8")
    config.CF_TOKEN_FILE = tmp / "cf-token.txt"
    real_requests, links.requests = links.requests, FakeCF()
    try:
        links.apply(db, [lk], {"links_per_site_per_day": 5}, {base: wpc}, rep)
        msg = reports[-1][2]
        purges = [c for c in links.requests.calls if c[0] == "POST"]
        check(reports[-1][1] == "applied" and "older cached copy" in msg and "see it by about" in msg
              and "not permitted" in msg,
              "a link hidden by the edge cache is reported as such, with when visitors will see it")
        check(purges and purges[0][1].endswith("/zones/zone1/purge_cache") and purges[0][2] == {"files": [lk["source"]]}
              and links.requests.calls[0][2] == {"Authorization": "Bearer selftest-cf-token-123"}
              and store.get_meta(db, base).get("cf_zone") == "zone1" and "selftest-cf-token-123" in util.SECRETS,
              "the cache purge uses only the app token, purges just that page, caches the zone and scrubs the token")
        n_rep = len(reports)
        links.recheck(db, rep)
        check(len(reports) == n_rep, "re-check while still cached: no repeated report")
        del EDGE["/good-indexed/"]
        links.recheck(db, rep)
        check(len(reports) == n_rep + 1 and "visible to visitors" in reports[-1][2]
              and "visible to visitors" in db.execute("SELECT result FROM link_ops WHERE id=?", (lk["id"],)).fetchone()[0],
              "once the cache refreshes, the re-check reports the link as visible to visitors")
        links.apply(db, [dict(lk, status="undo")], {}, {base: wpc}, rep)
        check(reports[-1][1] == "undone" and "cache purge: not permitted" in reports[-1][2], "undo also purges the page")
    finally:
        links.requests = real_requests
        config.CF_TOKEN_FILE = tmp / "no-token.txt"

    # ---- fresh-content autopilot
    store.set_meta(db, base, fresh_watermark="2026-10-01T00:00:00+00:00")
    FRESH.append(("/brand-new/", "2026-10-23T12:00:00"))
    ch = fresh.changed(db, sites)
    check(base in ch and [urlsplit(u).path for u, *_ in ch[base]] == ["/brand-new/"],
          "the fresh check finds a post published since the last look")
    fresh.ingest(db, ch)
    n_calls = len(inow.calls)
    submit.run(db, sites, bing_client=bng, bing_sites=bsites, indexnow_client=inow)
    check(len(inow.calls) == n_calls + 1 and base + "/brand-new/" in inow.calls[-1][3], "a brand-new post is submitted on the same tick")
    check(not fresh.changed(db, sites), "the watermark moves on: the same post is not picked up twice")

    # ---- regression alerts and Google transitions
    FLIP.add("/good-unknown/")
    audit.run(db, sites, force=True)
    kinds = [(r["kind"], r["url"]) for r in db.execute("SELECT kind, url FROM alerts")]
    check(any(k == "became_blocked" and u.endswith("/good-unknown/") for k, u in kinds), "a page that turns noindex raises an alert")
    FLIP.clear()
    audit.run(db, sites, force=True)
    check(any(r["kind"] == "unblocked" for r in db.execute("SELECT kind FROM alerts")), "its recovery raises a good-news alert")
    FakeGSC.COVERAGE["/good-discovered/"] = "Submitted and indexed"
    db.execute("UPDATE gsc SET inspected_at='2026-09-01T00:00:00+00:00' WHERE url LIKE '%/good-discovered/'")
    google.inspect_due(db, gsc, props)
    FakeGSC.COVERAGE["/good-discovered/"] = "Discovered - currently not indexed"
    check(db.execute("SELECT COUNT(*) FROM alerts WHERE kind='newly_indexed'").fetchone()[0] == 1,
          "a page entering Google's index raises a good-news alert")

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
