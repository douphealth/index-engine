# -*- coding: utf-8 -*-
"""Stage 2 - fetch every URL the way a crawler would and record what blocks indexing.

Per page: final status, redirect hops, noindex (meta or X-Robots-Tag), robots.txt rules for
Googlebot and bingbot, canonical target, main-content word count, a content sketch for
near-duplicate detection, and every internal link (to find orphans and plan internal links).
Read-only: plain GETs, a few per host at a time.
"""
import hashlib
import json
import re
import time
from collections import defaultdict, deque
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from html.parser import HTMLParser
from urllib import robotparser
from urllib.parse import urlsplit

from . import config, store
from .util import days_since, get, log, norm, now_iso, same_page, same_site

VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}
BOILER = {"nav", "header", "footer", "aside", "form", "script", "style", "noscript", "svg", "template", "iframe", "select"}
CONTENT_CLASS = re.compile(r"\b(entry-content|post-content|article-content|td-post-content|single-content|post-body|"
                           r"entry__content|wp-block-post-content|article-body|blog-post)\b", re.I)
WORD = re.compile(r"[^\W_]+(?:['’][^\W_]+)?", re.U)
SKIP_LINK = re.compile(r"/(wp-admin|wp-login|wp-json|feed|xmlrpc|cdn-cgi)(/|$)|\.(jpe?g|png|webp|gif|svg|avif|pdf|zip|css|js|xml)$", re.I)
SKETCH_K = 96
MAX_BYTES = 4_000_000


class PageParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack = []                      # (tag, is_content, is_boiler)
        self.content = self.boiler = 0
        self.saw_content = False
        self.title, self.h1 = [], []
        self.title_done = self.h1_done = False
        self.meta_robots, self.canonical = [], None
        self.links = []                      # (href, in_content)
        self.words_content, self.words_body = [], []

    def _in(self, tag):
        return any(t == tag for t, _, _ in self.stack)

    def handle_starttag(self, tag, attrs):
        a = {k: (v or "") for k, v in attrs}
        if tag == "meta":
            name = a.get("name", "").lower()
            if name in ("robots", "googlebot", "bingbot"):
                self.meta_robots.append((name, a.get("content", "").lower()))
            return
        if tag == "link":
            if "canonical" in a.get("rel", "").lower().split() and self.canonical is None:
                self.canonical = a.get("href", "").strip()
            return
        if tag in VOID:
            return
        if tag == "a" and a.get("href"):
            self.links.append((a["href"], self.content > 0 and self.boiler == 0))
        is_content = tag in ("article", "main") or bool(CONTENT_CLASS.search(a.get("class", "")))
        is_boiler = tag in BOILER
        self.stack.append((tag, is_content, is_boiler))
        self.content += is_content
        self.boiler += is_boiler
        self.saw_content |= is_content

    def handle_endtag(self, tag):
        if tag in VOID:
            return
        for i in range(len(self.stack) - 1, -1, -1):
            if self.stack[i][0] == tag:
                for t, c, b in self.stack[i:]:
                    self.content -= c
                    self.boiler -= b
                    if t == "h1" and self.h1:
                        self.h1_done = True
                    if t == "title" and self.title:
                        self.title_done = True
                del self.stack[i:]
                return

    def handle_data(self, data):
        top = self.stack[-1][0] if self.stack else ""
        if top == "title":
            if not self.title_done and not self._in("svg"):    # the document title, not an SVG icon's <title>
                self.title.append(data)
            return
        if self.boiler:
            return
        if not self.h1_done and self._in("h1"):
            self.h1.append(data)
        words = WORD.findall(data)
        if not words:
            return
        (self.words_content if self.content > 0 else self.words_body).extend(words)


def sketch(words):
    """Bottom-k sketch of 5-word shingles: compact, and comparable across pages for near-duplicates."""
    toks = [w.lower() for w in words[:6000]]
    hashes = set()
    for i in range(max(0, len(toks) - 4)):
        h = hashlib.blake2b(" ".join(toks[i:i + 5]).encode(), digest_size=8).digest()
        hashes.add(int.from_bytes(h, "big") >> 1)
    return sorted(hashes)[:SKETCH_K]


def robots_parser(robots_txt):
    rp = robotparser.RobotFileParser()
    rp.parse((robots_txt or "").splitlines())
    return rp


def _noindex(meta_robots, x_robots):
    for name, content in meta_robots:
        if name in ("robots", "googlebot") and re.search(r"\b(noindex|none)\b", content):
            return "meta"
    for part in (x_robots or "").lower().split(","):
        part = part.strip()
        if re.search(r"\b(noindex|none)\b", part) and (":" not in part or part.split(":")[0].strip() in ("googlebot", "all")):
            return "header"
    return None


def fetch_page(url, rp):
    rec = {"url": url, "checked_at": now_iso(), "status": None, "final_url": None, "hops": 0, "ctype": "",
           "ms": None, "noindex": 0, "noindex_src": None, "canonical": None, "canonical_state": None,
           "title": "", "h1": "", "words": 0, "sketch": "[]", "out_links": 0, "error": None, "links": []}
    rec["robots_google"] = int(rp.can_fetch("Googlebot", url))
    rec["robots_bing"] = int(rp.can_fetch("bingbot", url))
    t0 = time.time()
    try:
        r = get(url, stream=True, allow_redirects=True)
        body = r.raw.read(MAX_BYTES, decode_content=True)
        r.close()
    except Exception as e:
        rec["ms"] = int((time.time() - t0) * 1000)
        rec["error"] = type(e).__name__ + ": " + str(e)[:200]
        return rec
    rec["ms"] = int((time.time() - t0) * 1000)
    rec["status"], rec["final_url"], rec["hops"] = r.status_code, norm(r.url), len(r.history)
    rec["ctype"] = r.headers.get("Content-Type", "").split(";")[0].strip()
    x_robots = r.headers.get("X-Robots-Tag", "")
    link_hdr = r.headers.get("Link", "")
    if r.status_code in (403, 429, 503) and (r.headers.get("cf-mitigated") == "challenge" or b"Just a moment" in body[:5000]):
        rec["error"] = "bot challenge"           # a firewall answered, not the page; do not report it as broken
        return rec
    if "html" not in rec["ctype"]:
        src = _noindex([], x_robots)
        rec["noindex"], rec["noindex_src"] = int(bool(src)), src
        return rec

    enc = r.encoding if r.encoding and "charset" in r.headers.get("Content-Type", "").lower() else "utf-8"
    try:
        html = body.decode(enc, errors="replace")
    except LookupError:
        html = body.decode("utf-8", errors="replace")
    p = PageParser()
    try:
        p.feed(html)
        p.close()
    except Exception as e:
        rec["error"] = "parse: " + str(e)[:150]

    src = _noindex(p.meta_robots, x_robots)
    rec["noindex"], rec["noindex_src"] = int(bool(src)), src
    canonical = p.canonical
    if not canonical:
        m = re.search(r'<([^>]+)>\s*;\s*rel="?canonical"?', link_hdr, re.I)
        canonical = m.group(1) if m else None
    if canonical:
        rec["canonical"] = norm(canonical, base=r.url)
        rec["canonical_state"] = "self" if same_page(rec["canonical"], r.url) else "other"
    else:
        rec["canonical_state"] = "missing"
    rec["title"] = re.sub(r"\s+", " ", "".join(p.title)).strip()[:300]
    rec["h1"] = re.sub(r"\s+", " ", "".join(p.h1)).strip()[:300]
    main = p.words_content if p.saw_content and len(p.words_content) >= 50 else p.words_body + p.words_content
    rec["words"] = len(main)
    rec["sketch"] = json.dumps(sketch(main))

    links, site_host = {}, urlsplit(r.url).netloc
    for href, in_content in p.links:
        dst = norm(href, base=r.url)
        if not dst or not same_site(dst, site_host) or SKIP_LINK.search(urlsplit(dst).path) or same_page(dst, r.url):
            continue
        links[dst] = links.get(dst, False) or in_content
    rec["links"] = list(links.items())
    rec["out_links"] = len(links)
    return rec


def due_rows(db, sites, force=False, max_per_site=None):
    rows = db.execute(f"""SELECT u.url, u.site, u.lastmod, a.checked_at, a.lastmod_at_check
                          FROM urls u LEFT JOIN audits a ON a.url=u.url
                          WHERE (u.in_sitemap=1 OR u.in_wp=1) AND u.site IN ({','.join('?' * len(sites))})""",
                      list(sites)).fetchall()
    per_site = defaultdict(list)
    for r in rows:
        age = days_since(r["checked_at"])
        changed = r["lastmod"] and r["lastmod"] != r["lastmod_at_check"]
        if force or age is None or age > config.AUDIT_MAX_AGE_DAYS or changed:
            per_site[r["site"]].append((0 if age is None else 1, r["url"], r["site"], r["lastmod"]))
    out = {}
    for site, items in per_site.items():
        items.sort()
        out[site] = items[:max_per_site] if max_per_site else items
    return out


def save(db, rec, lastmod):
    links = rec.pop("links", [])
    rec["lastmod_at_check"] = lastmod
    cols = list(rec)
    db.execute(f"INSERT OR REPLACE INTO audits({','.join(cols)}) VALUES({','.join('?' * len(cols))})", [rec[c] for c in cols])
    db.execute("DELETE FROM links WHERE src=?", (rec["url"],))
    db.executemany("INSERT OR IGNORE INTO links(src, dst, in_content) VALUES(?,?,?)",
                   [(rec["url"], d, int(c)) for d, c in links])


def run(db, sites, force=False, max_per_site=None):
    work = due_rows(db, sites, force, max_per_site)
    total = sum(len(v) for v in work.values())
    if not total:
        log("[audit] nothing due")
        return
    log(f"[audit] {total} pages due across {len(work)} sites")
    parsers = {s: robots_parser(store.get_meta(db, s).get("robots", "")) for s in work}
    queues = {s: deque(items) for s, items in work.items()}
    limit = {s: config.SLOW_HOSTS.get(s, config.PER_HOST_CONCURRENCY) for s in work}
    active = defaultdict(int)
    inflight = {}
    done_n, t0 = 0, time.time()
    with ThreadPoolExecutor(max_workers=max(1, sum(limit.values()))) as ex:
        while queues or inflight:
            for s in list(queues):
                q = queues[s]
                while q and active[s] < limit[s]:
                    _, url, site, lastmod = q.popleft()
                    inflight[ex.submit(fetch_page, url, parsers[site])] = (site, lastmod)
                    active[s] += 1
                if not q:
                    del queues[s]
            if not inflight:
                break
            finished, _ = wait(inflight, return_when=FIRST_COMPLETED)
            for f in finished:
                site, lastmod = inflight.pop(f)
                active[site] -= 1
                save(db, f.result(), lastmod)
                done_n += 1
                if done_n % 100 == 0 or done_n == total:
                    db.commit()
                    rate = done_n / max(1e-6, time.time() - t0)
                    log(f"  audited {done_n}/{total}  ({rate:.1f}/s, ~{(total - done_n) / max(rate, 1e-6) / 60:.0f} min left)")
    db.commit()
