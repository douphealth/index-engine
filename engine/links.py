# -*- coding: utf-8 -*-
"""Internal-link autopilot: propose contextual links with natural anchors, apply the approved ones, undo on request.

Links from pages Google already indexes are the strongest lever this portfolio has for "discovered / unknown /
crawled, not indexed" pages. Rules that keep it safe:
  * the anchor is a phrase that ALREADY exists in the source page's own text (taken from the target's title);
    nothing is written but the <a> tag around it - no new sentences, blocks or boxes;
  * never inside headings, links, buttons, captions, quotes, tables, code, scripts or shortcodes;
  * only approved links are applied (approval in the dashboard), at most `links_per_site_per_day` per site
    (default 5, never above 10); sites in STRICT_SITES are never auto-approved (gearuptofit AGENTS.md rule 3/2);
  * raw content is backed up before each save, the result is verified in WordPress and on the live page,
    the change is logged, and Undo removes exactly the link that was added.
"""
import datetime as dt
import hashlib
import json
import re
import subprocess
import sys
import time
from collections import defaultdict
from pathlib import Path
from urllib.parse import urlsplit

import requests

from . import config, store
from .triage import STOP, clean_title
from .util import SECRETS, bare_host, get, log, now_iso

STRICT_SITES = {"gearuptofit.com"}
MAX_PER_DAY = 10
FORBIDDEN = {"a", "h1", "h2", "h3", "h4", "h5", "h6", "script", "style", "code", "pre", "button", "figcaption", "select",
             "textarea", "label", "summary", "title", "nav", "header", "footer", "aside", "figure", "table", "iframe",
             "noscript", "svg", "blockquote", "cite", "q", "option"}
VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}
TOKEN = re.compile(r"(<!--.*?-->|<![^>]*>|<[^>]+>|\[/?[A-Za-z][\w-]*[^\]\n]{0,300}\])", re.S)


def _backups():
    return config.OUT / "backups" / "links"


def segments(raw):
    """(start, end, safe) for every run of text in raw post content."""
    closing = set(m.group(1).lower() for m in re.finditer(r"\[/([A-Za-z][\w-]*)\]", raw))
    stack, pos, out = [], 0, []

    def safe():
        return not any(t in FORBIDDEN or t.startswith("sc:") for t in stack)

    for m in TOKEN.finditer(raw):
        if m.start() > pos:
            out.append((pos, m.start(), safe()))
        tok = m.group(0)
        if tok.startswith("[") and not tok.startswith("[/"):
            name = re.match(r"\[([A-Za-z][\w-]*)", tok).group(1).lower()
            if name in closing:
                stack.append("sc:" + name)
        elif tok.startswith("[/"):
            name = "sc:" + re.match(r"\[/([A-Za-z][\w-]*)", tok).group(1).lower()
            if name in stack:
                del stack[len(stack) - 1 - stack[::-1].index(name):]
        elif tok.startswith("<") and not tok.startswith("<!"):
            nm = re.match(r"</?\s*([A-Za-z][A-Za-z0-9]*)", tok)
            if nm:
                n = nm.group(1).lower()
                if tok.startswith("</"):
                    if n in stack:
                        del stack[len(stack) - 1 - stack[::-1].index(n):]
                elif not tok.rstrip().endswith("/>") and n not in VOID:
                    stack.append(n)
        pos = m.end()
    if pos < len(raw):
        out.append((pos, len(raw), safe()))
    return out


def phrases(title, common=frozenset()):
    """Anchor candidates from the target's title: 2-5 word runs with real topic words, longest first."""
    words = re.findall(r"[A-Za-z0-9][A-Za-z0-9'’-]*", clean_title(title))
    out = []
    for n in (5, 4, 3, 2):
        for i in range(len(words) - n + 1):
            run = words[i:i + n]
            topical = [w for w in run if w.lower() not in STOP and not w.isdigit() and len(w) > 2]
            if len(topical) < min(2, n) or not any(w.lower() not in common for w in topical):
                continue
            if run[0].lower() in STOP or run[-1].lower() in STOP:
                continue
            out.append(" ".join(run))
    return list(dict.fromkeys(out))


def links_to(html, target):
    """True when html contains an href to exactly this URL (with or without the trailing slash)."""
    return re.search(r"""href\s*=\s*["']""" + re.escape(target.rstrip("/")) + r"""/?["'#?]""", html or "") is not None


def find(raw, phrase, target):
    """First safe occurrence of phrase: (start, end, context) or None. Skips pages that already link to target."""
    if links_to(raw, target):
        return None
    rx = re.compile(r"(?<![\w-])" + r"\s+".join(map(re.escape, phrase.split())) + r"(?![\w-])", re.I)
    for s, e, ok in segments(raw):
        if not ok:
            continue
        m = rx.search(raw, s, e)
        if m:
            text = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", raw[max(s, m.start() - 160):min(e, m.end() + 120)])).strip()
            return m.start(), m.end(), text
    return None


def link_id(source, target):
    return hashlib.sha1(f"{source}|{target}".encode()).hexdigest()[:16]


def propose(db, results, actions, clients, per_site=20):
    """Natural-anchor proposals for the pages where links are expected to help most."""
    want = defaultdict(list)                      # site -> targets, best first
    for a in actions:
        if a["action"] == "add_links" and a["url"] not in want[a["site"]]:
            want[a["site"]].append(a["url"])
    titles = {r["url"]: r["title"] or "" for r in db.execute("SELECT url, title FROM audits")}
    gains = {(a["url"]): a["gain"] for a in actions if a["action"] == "add_links"}
    out = []
    for site, wpc in clients.items():
        plan = (results.get(site) or {}).get("plan") or []
        by_target = defaultdict(list)
        for p in plan:
            by_target[p["target"]].append(p)
        targets = want.get(site, []) + [t for t in by_target if t not in want.get(site, [])]
        df, n_titles = _df(titles, site)
        common = {w for w, n in df.items() if n > 0.25 * max(1, n_titles)}
        raw_cache, made = {}, 0
        for target in targets:
            if made >= per_site:
                break
            cands = phrases(titles.get(target, ""), common)
            if not cands:
                continue
            for p in sorted(by_target.get(target, []), key=lambda p: -p["similarity"]):
                src = p["source"]
                if src not in raw_cache:
                    try:
                        loc = wpc.find(src)
                        raw_cache[src] = (loc, wpc.raw(*loc)[0]) if loc else (None, None)
                    except Exception as e:
                        log(f"[links] {site}: cannot read {src}: {str(e)[:100]}")
                        raw_cache[src] = (None, None)
                loc, raw = raw_cache[src]
                if not raw:
                    continue
                # The anchor must name what is specific to the target, not the topic both pages share
                # ("Hawaiian spider plant" -> the Hawaiian page: yes; "spider plant" -> "make spider plant bushier": no).
                # ...and that word must be rare on this site, so the anchor says which page it leads to.
                distinct = _words(titles.get(target, "")) - _words(titles.get(src, "")) - common
                rare = max(3, 0.03 * n_titles)
                usable = [ph for ph in cands if any(df.get(w, 0) <= rare for w in _words(ph) & distinct)]
                hit = next(((ph, f) for ph in usable for f in [find(raw, ph, target)] if f), None)
                if not hit:
                    continue
                phrase, (s, e, context) = hit
                out.append({"id": link_id(src, target), "site": site, "source": src, "source_title": titles.get(src, ""),
                            "target": target, "target_title": titles.get(target, ""), "anchor": raw[s:e],
                            "context": context, "score": p["similarity"], "gain": gains.get(target, 0)})
                made += 1
                break                                  # one new link per target per run
        log(f"[links] {site}: {made} link proposals with natural anchors")
    return out


def _words(text):
    """Topic words of a title or phrase, numbers included (they name the page on a numerology or listicle site)."""
    return {w for w in re.findall(r"[a-z0-9]+", clean_title(text or "").lower()) if w not in STOP and (len(w) > 2 or w.isdigit())}


def _df(titles, site):
    """Word document frequency over this site's titles (brand and topic words appear on most of them)."""
    df, n = defaultdict(int), 0
    for u, t in titles.items():
        if site in u:
            n += 1
            for w in set(re.findall(r"[a-z0-9]+", (t or "").lower())):
                df[w] += 1
    return df, n


def _log_change(site, line):
    """gearuptofit: newest-first entry under today's heading in CHANGELOG-AGENTS.md (its rules require it)."""
    if site == "gearuptofit.com":
        p = config.PORTFOLIO / "01-gearuptofit" / "CHANGELOG-AGENTS.md"
        if p.exists():
            txt = p.read_text(encoding="utf-8")
            head = f"## {dt.datetime.now(dt.timezone.utc).date().isoformat()} (Index Engine link autopilot)\n\n"
            marker = "Read this before you start: it is how the next agent learns what the last one did.\n"
            if head in txt:
                txt = txt.replace(head, head + line + "\n", 1)
            elif marker in txt:
                txt = txt.replace(marker, marker + "\n" + head + line + "\n", 1)
            else:
                txt = None
            if txt is not None:
                p.write_text(txt, encoding="utf-8")
                return
    p = config.OUT / "changes.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "a", encoding="utf-8") as fh:
        fh.write(line + "\n")


CF_API = "https://api.cloudflare.com/client/v4"
EDGE_HOURS = 4          # longest edge-cache lifetime seen on these sites (gearuptofit AGENTS.md: about 4 h)


def purge(db, site, urls):
    """Clear Cloudflare's cached copy of these URLs, with the app's own token only (never other credentials).
    Returns 'purged' or why not. Needs the token to carry Zone > Cache Purge."""
    try:
        tok = config.CF_TOKEN_FILE.read_text(encoding="utf-8").split()[-1]
    except (OSError, IndexError):
        return "no app token"
    SECRETS.append(tok)
    h = {"Authorization": f"Bearer {tok}"}
    try:
        zid = store.get_meta(db, site).get("cf_zone")
        if not zid:
            z = requests.get(f"{CF_API}/zones", headers=h, params={"name": bare_host(urlsplit(config.site_base(site)).netloc)},
                             timeout=30).json().get("result") or []
            if not z:
                return "the site is not on this Cloudflare account"
            zid = z[0]["id"]
            store.set_meta(db, site, cf_zone=zid)
        r = requests.post(f"{CF_API}/zones/{zid}/purge_cache", headers=h, json={"files": urls}, timeout=30).json()
        if r.get("success"):
            return "purged"
        msg = str((r.get("errors") or [{}])[0].get("message", ""))[:80]
        return "not permitted (add Cache Purge to the app token)" if "auth" in msg.lower() or "permission" in msg.lower() else f"failed: {msg}"
    except Exception as e:
        return f"failed ({type(e).__name__})"


def visible_to_visitors(source, target):
    """Is the link on the page visitors and crawlers get (the plain URL, through any edge cache)?"""
    try:
        r = get(source, timeout=(10, 60))
        return links_to(r.text, target), r.headers.get("cf-cache-status"), r.headers.get("age")
    except Exception as e:
        return False, f"error {type(e).__name__}", None


def cached_msg(cache, age, purged=None):
    """Honest status for a link that is in WordPress but not yet in the copy Cloudflare serves."""
    try:
        cached_at = dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=int(age))
    except (TypeError, ValueError):
        cached_at = dt.datetime.now(dt.timezone.utc)
    eta = (cached_at + dt.timedelta(hours=EDGE_HOURS)).strftime("%H:%M UTC")
    return (f"applied; live in WordPress, but Cloudflare still serves an older cached copy (cache {cache}). "
            f"Visitors and Google see it by about {eta}" + (f". Cache purge: {purged}" if purged else ""))


def recheck(db, report):
    """Links applied in the last 2 days but not yet seen by visitors: look again, and say so when they are."""
    rows = db.execute("""SELECT *, julianday('now') - julianday(applied_at) AS days FROM link_ops
                         WHERE status='applied' AND COALESCE(result,'') NOT LIKE '%visible to visitors%'
                         AND julianday('now') - julianday(applied_at) < 3""").fetchall()
    for op in rows:
        seen, cache, age = visible_to_visitors(op["source"], op["target"])
        if not seen and cache in ("HIT", "STALE", "UPDATING", "REVALIDATED") and purge(db, op["site"], [op["source"]]) == "purged":
            time.sleep(2)                                # e.g. Cache Purge was added to the token since the link went in
            seen, cache, age = visible_to_visitors(op["source"], op["target"])
        if seen:
            msg = f"applied; visible to visitors (checked {now_iso()[11:16]} UTC)"
            log(f"[links] now visible to visitors: {op['source']} -> {op['target']}")
        elif op["days"] >= 2:
            msg = "applied in WordPress, but still not on the page visitors get after 2 days: open the post and check it"
        elif cache in ("HIT", "STALE", "UPDATING", "REVALIDATED"):
            msg = cached_msg(cache, age)
        else:
            continue
        if msg[:40] != (op["result"] or "")[:40]:          # report changes of state, not a re-estimated minute
            db.execute("UPDATE link_ops SET result=? WHERE id=?", (msg, op["id"]))
            report(op["id"], "applied", msg, op["applied_at"])
    db.commit()


def _site_guard_check():
    gate = config.SITE_GATES.get("gearuptofit.com")
    if not gate or not Path(gate).exists():
        return "site_guard not found"
    try:
        r = subprocess.run([sys.executable, str(gate), "check"], cwd=str(Path(gate).parent.parent), capture_output=True,
                           text=True, encoding="utf-8", errors="replace", timeout=1800)
        return {0: "CLEAN", 1: "ALERT", 2: "BLOCK"}.get(r.returncode, f"exit {r.returncode}")
    except Exception as e:
        return f"not run ({type(e).__name__})"


def apply(db, links, settings, clients, report):
    """Apply approved links and undo requested ones. report(id, status, result, applied_at) mirrors to the dashboard."""
    cap = max(0, min(MAX_PER_DAY, int(settings.get("links_per_site_per_day", 5) or 0)))
    autopilot = bool(settings.get("link_autopilot"))
    done = defaultdict(int)
    for r in db.execute("SELECT site FROM link_ops WHERE status='applied' AND julianday('now') - julianday(applied_at) < 1"):
        done[r["site"]] += 1
    touched = set()
    for lk in links:
        site, status = lk["site"], lk["status"]
        wpc = clients.get(site)
        if status == "undo":
            _undo(db, lk, wpc, report)
            continue
        auto = status == "proposed"
        if auto and (not autopilot or site in STRICT_SITES):
            continue
        if status not in ("approved", "proposed") or done[site] >= cap:
            continue
        if not wpc:
            report(lk["id"], "failed", "no WordPress login for this site", None)
            continue
        ok, msg = _apply_one(db, lk, wpc)
        report(lk["id"], "applied" if ok else "failed", msg, now_iso() if ok else None)
        if ok:
            done[site] += 1
            touched.add(site)
            _log_change(site, f"- {now_iso()[:16]}Z | Index Engine link autopilot, {'auto-approved' if auto else 'owner-approved in the dashboard'} | "
                              f"internal link on {lk['source']} -> {lk['target']} (anchor \"{lk['anchor']}\") | "
                              f"backup out/backups/links/{lk['id']}*.json; Undo in the dashboard | {msg}")
    if "gearuptofit.com" in touched:
        res = _site_guard_check()
        _log_change("gearuptofit.com", f"- {now_iso()[:16]}Z | Index Engine | site_guard.py check after the link batch: {res}")
        log(f"[links] gearuptofit site_guard check: {res}")
    db.commit()
    return touched


def _apply_one(db, lk, wpc):
    try:
        loc = wpc.find(lk["source"])
        if not loc:
            return False, "source is not a published post/page"
        raw, _ = wpc.raw(*loc)
        if links_to(raw, lk["target"]):
            return False, "the source already links to the target"
        hit = find(raw, lk["anchor"], lk["target"])
        if not hit:
            return False, "the anchor text is no longer in a safe place on the source page"
        s, e, _ = hit
        new = raw[:s] + f'<a href="{lk["target"]}">' + raw[s:e] + "</a>" + raw[e:]
        _backups().mkdir(parents=True, exist_ok=True)
        bk = _backups() / f"{lk['id']}-{dt.datetime.now():%Y%m%d-%H%M%S}.json"
        bk.write_text(json.dumps({"link": lk, "post": loc, "raw_before": raw}, ensure_ascii=False), encoding="utf-8")
        wpc.update(loc[0], loc[1], new)
        after, _ = wpc.raw(*loc)
        if f'<a href="{lk["target"]}">{raw[s:e]}</a>' not in after:
            return False, "saved, but the link is not in the stored content (check the post)"
        fresh = False
        for _ in range(2):                       # the server's own copy (cache bypassed)
            try:
                fresh = links_to(get(f"{lk['source']}?nc={int(time.time())}", timeout=(10, 60)).text, lk["target"])
                if fresh:
                    break
            except Exception:
                pass
            time.sleep(3)
        purged = purge(db, lk["site"], [lk["source"]])
        time.sleep(2)
        seen, cache, age = visible_to_visitors(lk["source"], lk["target"])
        if seen:
            msg = "applied; visible to visitors"
        elif fresh:
            msg = cached_msg(cache, age, purged)
        else:
            msg = "applied in WordPress; the page has not picked it up yet (page cache). Re-checked every tick"
        db.execute("INSERT OR REPLACE INTO link_ops VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                   (lk["id"], lk["site"], lk["source"], lk["target"], raw[s:e], loc[0], loc[1], "applied", now_iso(),
                    str(bk), msg))
        return True, msg
    except Exception as e:
        return False, f"{type(e).__name__}: {str(e)[:160]}"


def _undo(db, lk, wpc, report):
    op = db.execute("SELECT * FROM link_ops WHERE id=?", (lk["id"],)).fetchone()
    if not op or not wpc:
        report(lk["id"], "failed", "nothing recorded to undo on this PC", None)
        return
    try:
        raw, _ = wpc.raw(op["post_id"], op["post_type"])
        added = f'<a href="{op["target"]}">{op["anchor"]}</a>'
        if added not in raw:
            report(lk["id"], "undone", "the link was already gone", None)
        else:
            wpc.update(op["post_id"], op["post_type"], raw.replace(added, op["anchor"], 1))
            purged = purge(db, lk["site"], [op["source"]])
            report(lk["id"], "undone", f"link removed (cache purge: {purged})", None)
        db.execute("UPDATE link_ops SET status='undone' WHERE id=?", (lk["id"],))
        _log_change(lk["site"], f"- {now_iso()[:16]}Z | Index Engine link autopilot | UNDO: removed the link {op['source']} -> {op['target']}")
    except Exception as e:
        report(lk["id"], "failed", f"undo failed: {type(e).__name__}: {str(e)[:120]}", None)
