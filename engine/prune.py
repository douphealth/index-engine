# -*- coding: utf-8 -*-
"""The prune queue: carry out the prune plan's "noindex" and "redirect" decisions, one owner-approved page at a time.

The prune plan (engine/recovery.py) already decides, with evidence, which pages earn nothing on either engine. This
module turns the clear-cut ones into proposals on the dashboard; nothing happens until the owner approves a proposal.

  * noindex  - a page with 0 clicks on both engines, under MAX_IMPR impressions, at least MIN_AGE_DAYS old, and no other
               site linking to it (Bing's inbound-link count is known and 0). Served as "noindex, follow": Google drops the
               page and still follows its links, and the page leaves the sitemap.
  * redirect - a page with no search value that other sites link to: a 301 to the closest page Google shows, so the
               links keep counting.

WordPress has no API for either, so each site gets one small code snippet, the "Index Engine helper" (HELPER_CODE
below), through the Code Snippets plugin's REST API. It keeps every rule in one option (index_engine_rules) and does
nothing else; deactivating the snippet turns every rule off at once. The engine installs it the first time an approved
proposal needs it, checks the home page still answers afterwards, and switches it off again if it does not.

Each applied rule is verified on the live page (a cache-busting URL, then the plain one), recorded in prune_ops, and
can be undone from the dashboard. Sites with their own prune system or no Code Snippets plugin are left out (EXCLUDED).
"""
import hashlib
import time
import uuid
from collections import defaultdict
from urllib.parse import urlsplit

import requests

from . import config
from .util import log, norm, now_iso

MIN_AGE_DAYS = 120
MAX_IMPR = 20
MAX_PER_DAY = 100
DEFAULT_PER_DAY = 25
HELPER_NAME = "Index Engine helper"
HELPER_VERSION = 1
EXCLUDED = {
    "gearuptofit.com": "has its own owner-run prune in the child theme (gutf_prune_noindex_ids) and a release script; "
                       "a second noindex mechanism would hide pages that script releases",
    "plantastichaven.com": "has no Code Snippets plugin, so the helper cannot be installed",
}
ROUTE = "index-engine/v1/rules"

HELPER_CODE = r"""/**
 * Index Engine helper v1 - lets the Index Engine set "noindex, follow" on single posts/pages and 301 redirects, each one
 * approved by the site owner in the Index Engine dashboard. Every rule lives in the option `index_engine_rules`.
 * Deactivate this snippet to switch every rule off at once. It changes nothing else on the site.
 */
if (!function_exists('index_engine_rules')) {
    function index_engine_rules() {
        $r = get_option('index_engine_rules');
        if (!is_array($r)) { $r = array(); }
        foreach (array('noindex', 'redirects') as $k) {
            if (!isset($r[$k]) || !is_array($r[$k])) { $r[$k] = array(); }
        }
        return $r;
    }
    function index_engine_path($url) {
        $p = parse_url((string) $url, PHP_URL_PATH);
        $p = '/' . trim((string) $p, '/');
        return $p === '/' ? '/' : $p . '/';
    }
    function index_engine_noindexed() {
        if (!is_singular()) { return false; }
        $r = index_engine_rules();
        return isset($r['noindex'][(string) get_queried_object_id()]);
    }
    function index_engine_hidden_ids() {
        $r = index_engine_rules();
        $ids = array_map('intval', array_keys($r['noindex']));
        foreach ($r['redirects'] as $x) { if (!empty($x['id'])) { $ids[] = (int) $x['id']; } }
        return array_values(array_filter(array_unique($ids)));
    }
}
add_filter('wpseo_robots', function ($robots) { return index_engine_noindexed() ? 'noindex, follow' : $robots; }, 99);
add_filter('wpseo_robots_array', function ($robots) {
    if (index_engine_noindexed()) { $robots['index'] = 'noindex'; $robots['follow'] = 'follow'; }
    return $robots;
}, 99);
add_filter('wp_robots', function ($robots) {
    if (index_engine_noindexed()) { unset($robots['index']); $robots['noindex'] = true; $robots['follow'] = true; }
    return $robots;
}, 99);
add_filter('wpseo_exclude_from_sitemap_by_post_ids', function ($ids) {
    return array_merge(is_array($ids) ? $ids : array(), index_engine_hidden_ids());
});
add_filter('wp_sitemaps_posts_query_args', function ($args) {
    $args['post__not_in'] = array_merge(isset($args['post__not_in']) ? (array) $args['post__not_in'] : array(), index_engine_hidden_ids());
    return $args;
});
add_action('template_redirect', function () {
    $r = index_engine_rules();
    if (!empty($r['redirects'])) {
        $path = index_engine_path(isset($_SERVER['REQUEST_URI']) ? $_SERVER['REQUEST_URI'] : '/');
        if (isset($r['redirects'][$path]['to']) && $r['redirects'][$path]['to']) {
            wp_safe_redirect($r['redirects'][$path]['to'], 301, 'Index Engine');
            exit;
        }
    }
    if (index_engine_noindexed() && !headers_sent()) { header('X-Robots-Tag: noindex, follow', false); }
}, 0);
add_action('rest_api_init', function () {
    $owner = function () { return current_user_can('manage_options'); };
    register_rest_route('index-engine/v1', '/rules', array(
        array('methods' => 'GET', 'permission_callback' => $owner, 'callback' => function () {
            return rest_ensure_response(array('version' => 1, 'rules' => index_engine_rules()));
        }),
        array('methods' => 'POST', 'permission_callback' => $owner, 'callback' => function ($req) {
            $b = $req->get_json_params();
            if (!is_array($b)) { $b = array(); }
            $r = index_engine_rules();
            $now = gmdate('c');
            foreach ((array) (isset($b['add_noindex']) ? $b['add_noindex'] : array()) as $id) {
                $id = absint($id);
                if ($id && get_post($id)) { $r['noindex'][(string) $id] = $now; }
            }
            foreach ((array) (isset($b['remove_noindex']) ? $b['remove_noindex'] : array()) as $id) {
                unset($r['noindex'][(string) absint($id)]);
            }
            foreach ((array) (isset($b['add_redirects']) ? $b['add_redirects'] : array()) as $from => $x) {
                $to = esc_url_raw(is_array($x) && isset($x['to']) ? $x['to'] : '');
                $p = index_engine_path($from);
                if ($to && $p !== '/' && index_engine_path($to) !== $p) {
                    $r['redirects'][$p] = array('to' => $to, 'id' => absint(isset($x['id']) ? $x['id'] : 0), 'since' => $now);
                }
            }
            foreach ((array) (isset($b['remove_redirects']) ? $b['remove_redirects'] : array()) as $from) {
                unset($r['redirects'][index_engine_path($from)]);
            }
            update_option('index_engine_rules', $r, true);
            return rest_ensure_response(array('version' => 1, 'rules' => $r));
        }),
    ));
});
"""


def prune_id(url):
    return hashlib.sha1(f"prune|{url}".encode()).hexdigest()[:16]


def _path(url):
    p = "/" + (urlsplit(url).path or "/").strip("/")
    return "/" if p == "/" else p + "/"


# ---------------------------------------------------------------- proposals

def eligible(row, linked=frozenset()):
    """(action, why) for one prune-plan row, or None when it is not clear-cut enough to propose. Pure (tested)."""
    d = row.get("decision")
    if d not in ("noindex", "redirect") or row.get("url") in linked:
        return None
    clicks = (row.get("google_clicks_90d") or 0) + (row.get("bing_clicks") or 0)
    impr = (row.get("google_impr_90d") or 0) + (row.get("bing_impr") or 0)
    age, inbound = row.get("age_days"), row.get("inbound_links")
    if clicks or age is None or age < MIN_AGE_DAYS or impr >= MAX_IMPR:
        return None
    if d == "noindex":
        if inbound is None or inbound > 0:          # unknown inbound links: a 301 might be the right answer
            return None
        return "noindex", (f"0 clicks and {impr} impressions on both engines in 90 days, {age} days old, and Bing's link "
                           f"report shows no other site linking to it. {row.get('why', '')}").strip()
    if not row.get("target"):
        return None
    return "redirect", (f"0 clicks and {impr} impressions in 90 days, but {inbound} links from other sites point at it: a 301 "
                        "to the closest page Google shows keeps them counting.")


def propose(db, results, sites):
    """Proposals for the dashboard's prune queue, from each site's prune plan. Read-only."""
    linked = {r[0] for r in db.execute("SELECT DISTINCT target FROM link_ops WHERE status='applied'")}
    done = {r[0] for r in db.execute("SELECT url FROM prune_ops WHERE status='applied'")}
    out = []
    for site in sites:
        if site in EXCLUDED:
            continue
        plan = ((results.get(site) or {}).get("recovery") or {}).get("plan") or []
        n = defaultdict(int)
        for row in plan:
            got = eligible(row, linked | done)
            if not got:
                continue
            action, why = got
            n[action] += 1
            out.append({"id": prune_id(row["url"]), "site": site, "url": row["url"], "title": row.get("title") or "",
                        "action": action, "target": row.get("target") if action == "redirect" else None, "why": why,
                        "evidence": {k: row.get(k) for k in ("bucket", "flags", "google_impr_90d", "bing_impr", "words",
                                                             "age_days", "inbound_links")}})
        if n:
            log(f"[prune] {site}: {n['noindex']} pages to noindex and {n['redirect']} to redirect, waiting for approval")
    return out


# ---------------------------------------------------------------- the helper

def _wp(wpc, method, path, **kw):
    return wpc.s.request(method, f"{wpc.base}/wp-json/{path}", timeout=60, **kw)


def helper_rules(wpc):
    """The helper's rules, or None when the helper does not answer."""
    try:
        r = _wp(wpc, "GET", ROUTE)
        return r.json().get("rules") if r.status_code == 200 else None
    except (requests.RequestException, ValueError):
        return None


def _home_ok(wpc):
    try:
        r = requests.get(f"{wpc.base}/?nc={uuid.uuid4().hex[:8]}", headers={"User-Agent": config.UA}, timeout=40)
        return r.status_code == 200 and "critical error" not in r.text[:5000].lower()
    except requests.RequestException:
        return False


def install_helper(wpc, dry_run=False):
    """(ok, message). Installs or reactivates the helper snippet through Code Snippets, then checks the home page and
    the helper's API answer; if the home page breaks, the snippet is switched off again at once."""
    if wpc.site in EXCLUDED:
        return False, EXCLUDED[wpc.site]
    if helper_rules(wpc) is not None:
        return True, "helper already active"
    try:
        r = _wp(wpc, "GET", "code-snippets/v1/snippets")
        if r.status_code != 200:
            return False, f"Code Snippets API answered HTTP {r.status_code}"
        mine = [s for s in r.json() if (s.get("name") or "").strip() == HELPER_NAME]
        if dry_run:
            return True, "would " + ("reactivate the helper snippet" if mine else "install the helper snippet")
        body = {"name": HELPER_NAME, "code": HELPER_CODE, "scope": "global", "active": True, "priority": 10,
                "desc": "Noindex and 301 rules the Index Engine sets after the owner approves them. Deactivate to undo all."}
        if mine:
            sid = mine[0]["id"]
            r = _wp(wpc, "PUT", f"code-snippets/v1/snippets/{sid}", json=body)
        else:
            r = _wp(wpc, "POST", "code-snippets/v1/snippets", json=body)
            sid = (r.json() or {}).get("id") if r.status_code in (200, 201) else None
        if r.status_code not in (200, 201) or not sid:
            return False, f"Code Snippets refused the helper: HTTP {r.status_code} {r.text[:120]}"
        if not (r.json() or {}).get("active"):
            _wp(wpc, "POST", f"code-snippets/v1/snippets/{sid}/activate")
        time.sleep(2)
        if not _home_ok(wpc):
            _wp(wpc, "POST", f"code-snippets/v1/snippets/{sid}/deactivate")
            return False, "the home page did not answer normally after the helper was switched on; it was switched off again"
        if helper_rules(wpc) is None:
            return False, "the helper snippet is saved but its API does not answer (is the snippet active?)"
        log(f"[prune] {wpc.site}: Index Engine helper installed (Code Snippets #{sid})")
        return True, f"helper installed (Code Snippets #{sid})"
    except (requests.RequestException, ValueError) as e:
        return False, f"{type(e).__name__}: {str(e)[:120]}"


def _rules_call(wpc, body):
    r = _wp(wpc, "POST", ROUTE, json=body)
    if r.status_code != 200:
        raise RuntimeError(f"helper answered HTTP {r.status_code}: {r.text[:120]}")
    return r.json().get("rules") or {}


# ---------------------------------------------------------------- live checks

def _fetch(url, bust=True):
    u = url + (("&" if "?" in url else "?") + f"nc={uuid.uuid4().hex[:8]}" if bust else "")
    return requests.get(u, headers={"User-Agent": config.UA}, timeout=40, allow_redirects=False)


def served_noindex(resp):
    """True when the response tells search engines not to index it (meta robots or X-Robots-Tag)."""
    import re
    if "noindex" in (resp.headers.get("X-Robots-Tag") or "").lower():
        return True
    head = resp.text[:200_000]
    for m in re.finditer(r"<meta\b[^>]*>", head, re.I):
        tag = m.group(0).lower()
        if re.search(r"name\s*=\s*[\"']?(robots|googlebot)\b", tag) and "noindex" in tag:
            return True
    return False


def live_state(url, action, target=None):
    """(origin_ok, plain_ok): whether the rule shows on a cache-busting fetch and on the plain URL. None = no answer."""
    out = []
    for bust in (True, False):
        try:
            r = _fetch(url, bust)
        except requests.RequestException:
            out.append(None)
            continue
        if action == "noindex":
            out.append(r.status_code == 200 and served_noindex(r))
        else:
            loc = norm(requests.compat.urljoin(url, r.headers.get("Location", "")))
            out.append(r.status_code in (301, 308) and loc == norm(target))
    return tuple(out)


# ---------------------------------------------------------------- apply / undo

def apply(db, items, settings, clients, report):
    """Apply approved prune proposals and process undo requests. Never acts on a proposal the owner has not approved.
    report(id, status, result, applied_at) mirrors each outcome to the dashboard. Returns the sites changed."""
    cap = max(0, min(MAX_PER_DAY, int(settings.get("prunes_per_site_per_day", DEFAULT_PER_DAY) or 0)))
    done = defaultdict(int)
    for r in db.execute("SELECT site FROM prune_ops WHERE status='applied' AND julianday('now') - julianday(applied_at) < 1"):
        done[r["site"]] += 1
    ready, touched = {}, set()
    for it in items:
        site, wpc = it["site"], clients.get(it["site"])
        if it["status"] not in ("approved", "undo"):
            continue
        if site in EXCLUDED:
            report(it["id"], "failed", f"not done: {site} {EXCLUDED[site]}", None)
            continue
        if not wpc or not getattr(wpc, "ok", False):
            report(it["id"], "failed", "no WordPress login for this site", None)
            continue
        if it["status"] == "undo":
            if _undo(db, it, wpc, report):
                touched.add(site)
            continue
        if done[site] >= cap:
            continue
        if site not in ready:
            ready[site] = install_helper(wpc)
        ok, why = ready[site]
        if not ok:
            report(it["id"], "failed", f"the Index Engine helper is not available: {why}", None)
            continue
        ok, msg = _apply_one(db, it, wpc)
        report(it["id"], "applied" if ok else "failed", msg, now_iso() if ok else None)
        if ok:
            done[site] += 1
            touched.add(site)
    db.commit()
    return touched


def _apply_one(db, it, wpc):
    from .links import locate, purge
    try:
        loc = locate(db, wpc, it["url"])
        if not loc:
            return False, "not a published post or page any more"
        pid = loc[0]
        if it["action"] == "noindex":
            rules = _rules_call(wpc, {"add_noindex": [pid]})
            if str(pid) not in (rules.get("noindex") or {}):
                return False, "the helper did not store the rule"
        elif it["action"] == "redirect":
            if not it.get("target"):
                return False, "no redirect target"
            rules = _rules_call(wpc, {"add_redirects": {_path(it["url"]): {"to": it["target"], "id": pid}}})
            if _path(it["url"]) not in (rules.get("redirects") or {}):
                return False, "the helper did not store the redirect"
        else:
            return False, f"unknown action {it['action']}"
        purged = purge(db, it["site"], [it["url"]])
        origin, plain = live_state(it["url"], it["action"], it.get("target"))
        if origin is False:                         # stored, but the site does not serve it: take it back
            _rules_call(wpc, {"remove_noindex": [pid]} if it["action"] == "noindex" else {"remove_redirects": [_path(it["url"])]})
            return False, "stored, but the page did not serve it (another plugin overrides robots?); the rule was removed again"
        what = "served noindex, follow" if it["action"] == "noindex" else f"301 to {it['target']}"
        msg = what + {True: "; verified on the live page", None: "; the live page could not be checked",
                      False: "; the cached copy still shows the old page: re-checked every tick"}[plain]
        msg += f"; cache purge: {purged}"
        db.execute("INSERT OR REPLACE INTO prune_ops(id, site, url, action, target, post_id, status, applied_at, result, verified) "
                   "VALUES(?,?,?,?,?,?,?,?,?,?)", (it["id"], it["site"], it["url"], it["action"], it.get("target"), pid,
                                                  "applied", now_iso(), msg, int(bool(plain))))
        from .links import _log_change
        _log_change(it["site"], f"- {now_iso()[:16]}Z | Index Engine prune queue, owner-approved in the dashboard | "
                                f"{it['url']} | {msg} | Undo in the dashboard")
        return True, msg
    except Exception as e:
        return False, f"{type(e).__name__}: {str(e)[:160]}"


def _undo(db, it, wpc, report):
    op = db.execute("SELECT * FROM prune_ops WHERE id=?", (it["id"],)).fetchone()
    if not op:
        report(it["id"], "failed", "nothing recorded to undo on this PC", None)
        return False
    try:
        from .links import _log_change, purge
        body = {"remove_noindex": [op["post_id"]]} if op["action"] == "noindex" else {"remove_redirects": [_path(op["url"])]}
        _rules_call(wpc, body)
        purged = purge(db, it["site"], [op["url"]])
        db.execute("UPDATE prune_ops SET status='undone' WHERE id=?", (it["id"],))
        report(it["id"], "undone", f"rule removed; the page is served as before (cache purge: {purged})", None)
        _log_change(it["site"], f"- {now_iso()[:16]}Z | Index Engine prune queue | UNDO: {op['action']} removed from {op['url']}")
        return True
    except Exception as e:
        report(it["id"], "failed", f"undo failed: {type(e).__name__}: {str(e)[:120]}", None)
        return False


def recheck(db, report):
    """Rules applied in the last 3 days whose plain URL still served the old copy: look again and say what is true."""
    rows = db.execute("""SELECT * FROM prune_ops WHERE status='applied' AND verified=0
                         AND julianday('now') - julianday(applied_at) < 3""").fetchall()
    for op in rows:
        _origin, plain = live_state(op["url"], op["action"], op["target"])
        if plain:
            msg = (op["result"] or "").replace("the cached copy still shows the old page: re-checked every tick",
                                               "verified on the live page")
            db.execute("UPDATE prune_ops SET verified=1, result=? WHERE id=?", (msg, op["id"]))
            report(op["id"], "applied", msg, op["applied_at"])
    db.commit()

