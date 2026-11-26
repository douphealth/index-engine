# -*- coding: utf-8 -*-
"""Cleanup queue: remove the automated-content scaffolding Google's scaled-content systems judge, one approved post at a
time, with a backup, verification and undo. The fixing half of engine/recovery.py's TEMPLATE_ARTIFACT findings.

What it proposes to cut (whole sections: the heading and everything up to the next heading of the same or a higher level):
  numbered     3+ headings on one post that differ only by a number and say more than "Week N"
               ("Use-case notes for ASICS Gel-Nimbus 27 review #1" ... "#7")
  leftover     a heading an automated pipeline left behind ("Visual Examples From the WordPress Media Library")
  boilerplate  a section whose text is near-identical (shingle overlap >= 80%) on at least 5 posts of the site, so cutting
               it loses nothing unique. A section with the same heading but its own content (a real "Direct answer") is kept.
Safety rules:
  * nothing is applied without the owner's approval in the dashboard, unless the owner switched on Autonomous mode;
    either way at most `cleanups_per_site_per_day` posts per site per day (default 10, never above 30): every save
    re-dates a post. Autonomous mode adds two brakes per site: a canary (until one cleanup is confirmed gone from the
    live page, at most one autonomous cleanup a day) and a circuit breaker (two failed autonomous cleanups within 24
    hours stop autonomous cleanups on that site for 24 hours and raise an urgent alert). gearuptofit is left out of
    Autonomous mode unless the owner includes it explicitly (its AGENTS.md requires the owner's click);
  * a section is cut only when it is cleanly separable: it ends at a later heading of the same or a higher level (or is
    short), and the cut keeps every HTML element and Gutenberg block balanced;
  * the post is re-read at apply time and the sections are found again by their heading text; the raw content is backed up
    first; after saving, WordPress must have stored exactly the cleaned content (or the original is restored), and the
    page the server sends (cache bypassed) is checked: each cut heading must appear fewer times than before the save.
    When it does not yet (page cache, or a page built with Elementor that ignores the post body) it is re-checked on
    every tick and the result says so after two days;
  * gearuptofit: CHANGELOG-AGENTS.md entry and site_guard check after the batch (its rules);
  * Undo restores the backup, only when the post has not been edited since.
"""
import datetime as dt
import hashlib
import html as htmlmod
import json
import re
import time
from collections import Counter, defaultdict

from . import alerts, config, memo, store
from .links import _log_change, _site_guard_check, locate, purge
from .recovery import GENERIC, LEFTOVERS, NUMBERED_MIN_WORDS, NUMBERED_RUN, _shape
from .util import get, log, now_iso

MAX_PER_DAY = 30
DEFAULT_PER_DAY = 10
BOILER_MIN_POSTS = 5
BOILER_SIMILAR = 0.8
OPEN_SECTION_MAX_WORDS = 300       # a last section (no heading after it) is cut only when it is this short
MAX_SECTION_WORDS = 1500           # never cut a section longer than this in one go: that is a rewrite, not a cleanup
SHINGLE = 5

HEAD = re.compile(r"<h([1-6])\b[^>]*>(.*?)</h\1\s*>", re.I | re.S)
WP_OPEN_BEFORE = re.compile(r"<!--\s*wp:heading\b[^>]*-->\s*$", re.S)
WP_CLOSE_AFTER = re.compile(r"^\s*<!--\s*/wp:heading\s*-->", re.S)
TAGS = re.compile(r"<!--\s*(/?)wp:([a-z0-9/-]+)[^>]*?(/?)\s*-->|<(/?)([a-zA-Z][a-zA-Z0-9]*)\b[^>]*?(/?)>", re.S)
VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}
# Tags whose omitted end tags HTML allows (a <p> or <li> left open by an editor must not count as unbalanced).
OPTIONAL_END = {"p", "li", "dt", "dd", "tr", "td", "th", "thead", "tbody", "tfoot", "option"}


def _text(fragment):
    return re.sub(r"\s+", " ", htmlmod.unescape(re.sub(r"<[^>]+>", " ", fragment or ""))).strip()


def _norm_heading(t):
    return re.sub(r"[^\w\s&-]", "", t.lower()).strip()


def balanced(fragment):
    """Does cutting this fragment out leave the document's elements and Gutenberg blocks intact?"""
    stack = []
    for m in TAGS.finditer(fragment):
        if m.group(2):                                  # Gutenberg block comment
            closing, name, selfclose = m.group(1), "wp:" + m.group(2), m.group(3)
        else:
            closing, name, selfclose = m.group(4), m.group(5).lower(), m.group(6)
            if name in VOID or name in OPTIONAL_END:
                continue
        if selfclose:
            continue
        if not closing:
            stack.append(name)
        elif stack and stack[-1] == name:
            stack.pop()
        else:
            return False                                # closes something opened before the cut
    return not stack


def sections(raw):
    """Every h2-h6 section of a post: [{level, heading, start, end, bounded, body}] with start/end in raw. A section runs
    from its heading (and the Gutenberg comment around it) to the next heading of the same or a higher level."""
    heads = []
    for m in HEAD.finditer(raw):
        lvl = int(m.group(1))
        s, e = m.start(), m.end()
        before = WP_OPEN_BEFORE.search(raw[max(0, s - 300):s])
        if before:
            s = max(0, s - 300) + before.start()
        after = WP_CLOSE_AFTER.match(raw[e:e + 100])
        if after:
            e += after.end()
        heads.append((lvl, _text(m.group(2)), s, e))
    out = []
    for i, (lvl, text, s, e) in enumerate(heads):
        if lvl == 1:
            continue
        nxt = next((h for h in heads[i + 1:] if h[0] <= lvl), None)
        end = nxt[2] if nxt else len(raw)
        out.append({"level": lvl, "heading": text, "start": s, "end": end, "bounded": nxt is not None,
                    "body": raw[e:end]})
    return out


def _shingles(text):
    """Every run of SHINGLE consecutive words (a shorter text is one shingle), as a set."""
    w = re.findall(r"[a-z0-9]+", text.lower())
    if len(w) <= SHINGLE:
        return {tuple(w)} if w else set()
    return set(zip(*(w[i:] for i in range(SHINGLE))))


def _similar(a, b):
    return len(a & b) / max(1, min(len(a), len(b))) if a and b else 0.0


def _repeated(mine, others):
    """Is this section's text near-identical on at least BOILER_MIN_POSTS posts? Stops counting once it is."""
    n = 0
    for other in others:
        if _similar(mine, other) >= BOILER_SIMILAR:
            n += 1
            if n >= BOILER_MIN_POSTS:
                return True
    return False


def _section_shingles(sec):
    """The shingles of a section's text, computed once per section however often it is compared."""
    sh = sec.get("_shingles")
    if sh is None:
        sh = sec["_shingles"] = _shingles(_text(sec["body"]))
    return sh


def _cuttable(sec):
    words = len(_text(sec["body"]).split())
    if words > MAX_SECTION_WORDS:
        return False, words
    if not sec["bounded"] and words > OPEN_SECTION_MAX_WORDS:
        return False, words
    return True, words


def find_cuts(raw, boiler=None, secs=None):
    """Sections of one post to cut, each {heading, kind, words, excerpt}. boiler: {normalized heading: [shingle sets]} of
    section texts repeated across the site (see boilerplate_index). secs: this post's sections(raw) when the caller has
    them already (a site scan parses each post once)."""
    secs = sections(raw) if secs is None else secs
    shapes = Counter(_shape(s["heading"]) for s in secs if re.search(r"\d", s["heading"]))
    template = {sh for sh, n in shapes.items() if n >= NUMBERED_RUN and len(re.findall(r"[a-z]+", sh)) >= NUMBERED_MIN_WORDS}
    cuts, taken = [], []
    for sec in secs:
        if any(a <= sec["start"] < b for a, b in taken):       # inside a section already being cut
            continue
        kind = None
        if re.search(r"\d", sec["heading"]) and _shape(sec["heading"]) in template:
            kind = "numbered"
        elif LEFTOVERS.search(sec["heading"]):
            kind = "leftover"
        elif boiler:
            key = _norm_heading(sec["heading"])
            if key in boiler and key not in GENERIC:
                mine = _section_shingles(sec)
                if len(mine) >= 3 and _repeated(mine, boiler[key]):
                    kind = "boilerplate"
        if not kind:
            continue
        ok, words = _cuttable(sec)
        if not ok or not balanced(raw[sec["start"]:sec["end"]]):
            continue
        taken.append((sec["start"], sec["end"]))
        cuts.append({"heading": sec["heading"], "kind": kind, "words": words,
                     "excerpt": _text(sec["body"])[:180]})
    return cuts


def boilerplate_index(posts, parsed=None):
    """{normalized heading: [shingle set of the section text, one per post]} for headings on BOILER_MIN_POSTS+ posts.
    parsed: the posts' sections, in the same order, when the caller has parsed them already."""
    by = defaultdict(list)
    for secs in (parsed if parsed is not None else map(sections, posts)):
        seen = set()
        for sec in secs:
            key = _norm_heading(sec["heading"])
            if key in seen or key in GENERIC or len(re.findall(r"[^\W\d_]{3,}", key)) < 2:
                continue
            seen.add(key)
            by[key].append(_section_shingles(sec))
    return {k: v for k, v in by.items() if len(v) >= BOILER_MIN_POSTS}


def cleanup_id(url):
    return hashlib.sha1(f"cleanup|{url}".encode()).hexdigest()[:16]


SITE_CUTS_VERSION = "site-cuts-1"     # change it whenever what find_cuts decides changes: cached site scans are redone


def site_cuts(posts):
    """[[url, cuts]] for every post of one site that has something to cut. posts: {url: raw content}. Each post is parsed
    once; the result depends only on the posts, so engine/memo.py keeps it until one of them changes."""
    parsed = {url: sections(raw) for url, raw in posts.items()}
    boiler = boilerplate_index(None, parsed.values())
    out = []
    for url, raw in posts.items():
        cuts = find_cuts(raw, boiler, parsed[url])
        if cuts:
            out.append([url, cuts])
    return out


def propose(db, results, sites):
    """One proposal per post that has something to cut, for every site with stored WordPress content. Pages the triage
    left out (blocked or ignored by the owner) are skipped. Returns the list for the dashboard."""
    out = []
    for site in sites:
        found = memo.per_site(db, site, "site_cuts", SITE_CUTS_VERSION, site_cuts)
        if not found:
            continue
        buckets = (results.get(site) or {}).get("buckets") or {}
        titles = {r[0]: r[1] for r in db.execute("SELECT a.url, a.title FROM audits a JOIN urls u ON u.url=a.url WHERE u.site=?", (site,))}
        n = 0
        for url, cuts in found:
            b = buckets.get(url)
            if b and "IGNORED" in b[1]:
                continue
            n += 1
            out.append({"id": cleanup_id(url), "site": site, "url": url, "title": titles.get(url) or "",
                        "sections": cuts, "words": sum(c["words"] for c in cuts)})
        if n:
            log(f"[cleanup] {site}: {n} posts with automated-content scaffolding to cut")
    out.sort(key=lambda x: -x["words"])
    return out


# ---------------------------------------------------------------- apply / undo

def _backups():
    return config.OUT / "backups" / "cleanup"


def _digest(raw):
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def cut(raw, wanted):
    """Remove the sections whose headings are in `wanted`, re-found in this raw (positions move when a post is edited).
    A section that is no longer cleanly separable is left alone. Returns (new raw, cut headings)."""
    keys = {_norm_heading(w["heading"]) for w in wanted}
    spans = []
    for sec in sections(raw):
        if _norm_heading(sec["heading"]) not in keys or any(a <= sec["start"] < b for a, b in spans):
            continue
        ok, _ = _cuttable(sec)
        if ok and balanced(raw[sec["start"]:sec["end"]]):
            spans.append((sec["start"], sec["end"]))
    new, done = raw, []
    for s, e in sorted(spans, reverse=True):
        done.append(_text(HEAD.search(raw[s:e]).group(2)) if HEAD.search(raw[s:e]) else "")
        new = new[:s].rstrip() + "\n\n" + new[e:].lstrip()
    return new, done


def live_counts(url, headings):
    """How many times each heading's text appears on the page the server sends (edge cache bypassed); None if unreachable."""
    try:
        page = _text(get(f"{url}{'&' if '?' in url else '?'}nc={int(time.time())}", timeout=(10, 60)).text).lower()
    except Exception:
        return None
    return {h: page.count(h.lower()) for h in headings if h}


def gone_live(before, after):
    """Every cut heading shows fewer times than before the save (a heading also in a menu or sidebar still counts)."""
    if not before or after is None:
        return None
    return all(after.get(h, 0) < n for h, n in before.items() if n)


STRICT_SITES = {"gearuptofit.com"}
BREAKER_FAILS = 2
BREAKER_HOURS = 24


def autonomous_for(settings, site):
    """May the engine act on this site without the owner's click?"""
    return bool(settings.get("autonomous")) and (site not in STRICT_SITES or bool(settings.get("autonomous_strict_sites")))


def breaker(db, site):
    """The open circuit breaker of this site ({"at", "why"}) or None."""
    b = store.get_meta(db, site).get("cleanup_breaker")
    if b and (dt.datetime.now(dt.timezone.utc) - dt.datetime.fromisoformat(b["at"])).total_seconds() < BREAKER_HOURS * 3600:
        return b
    return None


def _trip(db, site, why):
    store.set_meta(db, site, cleanup_breaker={"at": now_iso(), "why": why})
    alerts.add(db, site, None, "autonomous_paused", "high",
               f"Autonomous cleanups on {site} stopped for {BREAKER_HOURS} hours after {BREAKER_FAILS} failures in a row "
               f"(last: {why[:160]}). Approved cleanups still run; check the failed ones on the Recovery page.")
    log(f"[cleanup] {site}: circuit breaker open: {why[:120]}")


def _verified_before(db, site):
    return db.execute("SELECT 1 FROM cleanup_ops WHERE site=? AND result LIKE '%gone from the live page%' LIMIT 1",
                      (site,)).fetchone() is not None


def apply(db, items, settings, clients, report):
    """Apply approved cleanups (and, in Autonomous mode, proposed ones) and process undo requests.
    report(id, status, result, applied_at) mirrors to the dashboard."""
    cap = max(0, min(MAX_PER_DAY, int(settings.get("cleanups_per_site_per_day", DEFAULT_PER_DAY) or 0)))
    done, auto_done = defaultdict(int), defaultdict(int)
    for r in db.execute("SELECT site, auto FROM cleanup_ops WHERE status='applied' AND julianday('now') - julianday(applied_at) < 1"):
        done[r["site"]] += 1
        auto_done[r["site"]] += 1 if r["auto"] else 0
    fails = defaultdict(int)
    touched = set()
    for it in items:
        site, wpc = it["site"], clients.get(it["site"])
        if it["status"] == "undo":
            _undo(db, it, wpc, report)
            continue
        auto = it["status"] == "proposed"
        if auto:
            if not autonomous_for(settings, site) or breaker(db, site):
                continue
            if not _verified_before(db, site) and auto_done[site] >= 1:      # canary: one a day until one is verified live
                continue
        elif it["status"] != "approved":
            continue
        if done[site] >= cap:
            continue
        if not wpc:
            if not auto:
                report(it["id"], "failed", "no WordPress login for this site", None)
            continue
        ok, msg = _apply_one(db, it, wpc, auto)
        report(it["id"], "applied" if ok else "failed", msg, now_iso() if ok else None)
        if ok:
            done[site] += 1
            auto_done[site] += 1 if auto else 0
            fails[site] = 0
            touched.add(site)
            _log_change(site, f"- {now_iso()[:16]}Z | Index Engine cleanup queue, "
                              f"{'Autonomous mode' if auto else 'owner-approved in the dashboard'} | {it['url']} | "
                              f"{msg} | backup out/backups/cleanup/{it['id']}*.json; Undo in the dashboard")
        elif auto:
            fails[site] += 1
            if fails[site] >= BREAKER_FAILS:
                _trip(db, site, msg)
    if "gearuptofit.com" in touched:
        res = _site_guard_check()
        _log_change("gearuptofit.com", f"- {now_iso()[:16]}Z | Index Engine | site_guard.py check after the cleanup batch: {res}")
        log(f"[cleanup] gearuptofit site_guard check: {res}")
    db.commit()
    return touched


def _apply_one(db, it, wpc, auto=False):
    try:
        wanted = it.get("sections")
        if isinstance(wanted, str):
            wanted = json.loads(wanted or "[]")
        loc = locate(db, wpc, it["url"])
        if not loc:
            return False, "not a published post/page any more"
        raw, _ = wpc.raw(*loc)
        new, gone = cut(raw, wanted or [])
        if not gone:
            return False, "none of the sections is still on the post in a cleanly removable form"
        before = live_counts(it["url"], gone)
        _backups().mkdir(parents=True, exist_ok=True)
        bk = _backups() / f"{it['id']}-{dt.datetime.now():%Y%m%d-%H%M%S}.json"
        bk.write_text(json.dumps({"item": {k: it.get(k) for k in ("id", "site", "url")}, "post": loc, "raw_before": raw},
                                 ensure_ascii=False), encoding="utf-8")
        wpc.update(loc[0], loc[1], new)
        after, _ = wpc.raw(*loc)
        left = Counter(_norm_heading(x["heading"]) for x in sections(after))
        had = Counter(_norm_heading(x["heading"]) for x in sections(raw))
        if after == raw or any(left[_norm_heading(g)] >= had[_norm_heading(g)] for g in gone):
            wpc.update(loc[0], loc[1], raw)
            return False, "WordPress did not store the cleaned content; the original was restored"
        purged = purge(db, it["site"], [it["url"]])
        seen = None
        for _ in range(3):
            seen = gone_live(before, live_counts(it["url"], gone))
            if seen is not False:
                break
            time.sleep(3)
        what = (f"cut {len(gone)} section{'s' if len(gone) != 1 else ''} ({'; '.join(g[:50] for g in gone[:3])}"
                f"{'…' if len(gone) > 3 else ''})")
        msg = what + {True: "; gone from the live page", None: "; the live page could not be checked",
                      False: "; saved in WordPress, the live page still shows them (page cache?): re-checked every tick"}[seen]
        msg += f"; cache purge: {purged}"
        db.execute("INSERT OR REPLACE INTO cleanup_ops(id, site, url, post_id, post_type, status, applied_at, backup, after_hash, "
                   "result, gone, live_before, auto) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                   (it["id"], it["site"], it["url"], loc[0], loc[1], "applied", now_iso(), str(bk), _digest(after), msg,
                    json.dumps(gone), json.dumps(before) if before else None, int(auto)))
        return True, msg
    except Exception as e:
        return False, f"{type(e).__name__}: {str(e)[:160]}"


def recheck(db, report):
    """Cleanups applied in the last 3 days that the live page did not reflect yet: look again, and say what is true."""
    rows = db.execute("""SELECT *, julianday('now') - julianday(applied_at) AS days FROM cleanup_ops
                         WHERE status='applied' AND result LIKE '%re-checked every tick%' AND live_before IS NOT NULL
                         AND julianday('now') - julianday(applied_at) < 3""").fetchall()
    for op in rows:
        gone = json.loads(op["gone"] or "[]")
        seen = gone_live(json.loads(op["live_before"]), live_counts(op["url"], gone))
        if seen:
            msg = op["result"].replace("saved in WordPress, the live page still shows them (page cache?): re-checked every tick",
                                       "gone from the live page")
        elif seen is False and op["days"] >= 2:
            msg = op["result"].replace("re-checked every tick", "still shown after 2 days: the page is probably built with a page "
                                       "builder (Elementor) that ignores the post body. Undo restores the old version")
        else:
            continue
        db.execute("UPDATE cleanup_ops SET result=? WHERE id=?", (msg, op["id"]))
        report(op["id"], "applied", msg, op["applied_at"])
    db.commit()


def _undo(db, it, wpc, report):
    op = db.execute("SELECT * FROM cleanup_ops WHERE id=?", (it["id"],)).fetchone()
    if not op or not wpc:
        report(it["id"], "failed", "nothing recorded to undo on this PC", None)
        return
    try:
        before = json.loads(open(op["backup"], encoding="utf-8").read())["raw_before"]
        raw, _ = wpc.raw(op["post_id"], op["post_type"])
        if _digest(raw) != op["after_hash"]:
            report(it["id"], "failed", "the post was edited after the cleanup; restore it by hand from the backup " + op["backup"], None)
            return
        wpc.update(op["post_id"], op["post_type"], before)
        purged = purge(db, it["site"], [op["url"]])
        db.execute("UPDATE cleanup_ops SET status='undone' WHERE id=?", (it["id"],))
        report(it["id"], "undone", f"original content restored (cache purge: {purged})", None)
        _log_change(it["site"], f"- {now_iso()[:16]}Z | Index Engine cleanup queue | UNDO: restored {op['url']} from {op['backup']}")
    except Exception as e:
        report(it["id"], "failed", f"undo failed: {type(e).__name__}: {str(e)[:120]}", None)
