# -*- coding: utf-8 -*-
"""micegoneguide.com: retire /rodent-proofing-for-new-constructions/ (post 928) into /mouse-proofing/ (page 3160).
Owner-approved 23 Oct 2026: "draft /rodent-proofing-for-new-constructions/ on micegoneguide and 301 it to /mouse-proofing/".

    python mgg_consolidate_rodent_proofing_20261023.py                       # dry run: checks + plan, writes nothing
    python mgg_consolidate_rodent_proofing_20261023.py --apply               # backup, redirect, draft, purge, verify, log
    python mgg_consolidate_rodent_proofing_20261023.py --rollback <backup.json>

Why: the post has been quarantined with noindex since 31 Jul 2026 but stayed in the Squirrly sitemap (mixed signals).
Bing (checked 23 Oct): 7 clicks, all Jul-Oct 2025, none in 2026. Google: "Discovered - currently not indexed".
The 9 Oct audit and the Sep changelog both recommend this consolidation.

What --apply does, in this order (each step verified before the next):
  1. JSON backup of post 928 (full edit context) and of every snippet that mentions the path.
  2. New Code Snippet, a copy of the pattern in snippet #154: exact path -> 301 -> /mouse-proofing/, on template_redirect.
     The site's redirects already live in Code Snippets (#28, #29, #87, #108, #154); the Redirection plugin has 0 rules.
     Activated and proven on the live URL BEFORE the post changes, so the URL is never a 404. Fails -> snippet removed, stop.
  3. Post 928 -> draft (guardrail 2: no redirect over a published post). Content is untouched.
  4. Cloudflare purge (this site caches HTML at the edge for hours) of the old URL, the sitemaps and the four pages whose
     related-posts block or previous/next link pointed at the post (they drop it by themselves once it is a draft).
  5. Verify through Cloudflare: old URL -> one 301 -> /mouse-proofing/ -> 200 index; post gone from sitemap-posts.xml.
     A failed check rolls everything back.
  6. Tell Bing the URL changed (SubmitUrlBatch, this one URL) so it re-crawls and follows the 301.
  7. Append the change to 05-micegoneguide/CHANGELOG-2026-10.md.
Credentials are loaded inside the helpers and never printed.
"""
import argparse
import datetime as dt
import hashlib
import json
import re
import sys
import time
from pathlib import Path

import requests

sys.path.insert(0, r"D:\Claude\SEO-Setup\portfolio-audit-2026-09-17\impl")
sys.path.insert(0, r"D:\Claude\SEO-Setup\05-micegoneguide\ops")
sys.path.insert(0, r"D:\Claude\SEO-Setup\index-engine")
from wpkit import WP  # noqa: E402
from creds import cf_purge  # noqa: E402

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
HERE = Path(__file__).resolve().parent
SITE = "micegoneguide.com"
HOME = f"https://{SITE}"
POST_ID, SLUG = 928, "rodent-proofing-for-new-constructions"
TARGET_ID, TARGET = 3160, f"{HOME}/mouse-proofing/"
SOURCE = f"{HOME}/{SLUG}/"
LINKING_PAGES = [f"{HOME}/{p}/" for p in ("can-mice-climb-walls", "identifying-mouse-nests-in-your-home", "mice-allergy-risks", "safely-remove-mice")]
SITEMAPS = [f"{HOME}/sitemap-posts.xml", f"{HOME}/sitemap.xml", f"{HOME}/sitemap_index.xml"]
SNIPPET_NAME = "MGG owner-approved consolidation: rodent-proofing-for-new-constructions -> mouse-proofing (2026-10-23)"
SNIPPET_CODE = """/**
 * Owner-approved 23 Oct 2026: retire the quarantined post 928 into /mouse-proofing/.
 * Bing: 7 clicks Jul-Oct 2025, none in 2026. Exact source path only (same pattern as snippet #154).
 * Rollback: deactivate this snippet and republish post 928.
 */
if (!defined('ABSPATH')) { exit; }
add_action('template_redirect', function() {
    if (is_admin() || wp_doing_ajax()) { return; }
    $path = parse_url($_SERVER['REQUEST_URI'] ?? '/', PHP_URL_PATH);
    if (rtrim((string) $path, '/') !== '/rodent-proofing-for-new-constructions') { return; }
    wp_safe_redirect(home_url('/mouse-proofing/'), 301, 'MGG owner-approved consolidation 2026-10-23');
    exit;
}, -20);
"""
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0 Safari/537.36"}


def stamp():
    return dt.datetime.now().strftime("%Y%m%d-%H%M%S")


def get(url, bust=False):
    if bust:
        url += ("&" if "?" in url else "?") + f"_v={time.time_ns()}"
    return requests.get(url, headers=UA, timeout=60, allow_redirects=False)


def robots(html):
    return re.findall(r"<meta[^>]+name=['\"]robots['\"][^>]*content=['\"]([^'\"]*)", html, re.I)


def source_view(bust):
    """(status, location) of the old URL, through Cloudflare (bust=False) or past its cache (bust=True)."""
    r = get(SOURCE, bust)
    return r.status_code, r.headers.get("location"), r.headers.get("x-redirect-by")


def target_ok():
    r = get(TARGET, bust=True)
    rb = robots(r.text)
    canon = re.findall(r"<link[^>]+rel=['\"]canonical['\"][^>]*href=['\"]([^'\"]*)", r.text, re.I)
    ok = r.status_code == 200 and not any("noindex" in x for x in rb) and canon[:1] == [TARGET]
    return ok, {"status": r.status_code, "robots": rb, "canonical": canon[:1]}


def in_sitemap():
    return SOURCE in get(f"{HOME}/sitemap-posts.xml", bust=True).text


def snippets_mentioning(wp):
    out = []
    for s in wp.get("code-snippets/v1/snippets"):
        if f"/{SLUG}" in (s.get("code") or "") or SLUG in (s.get("code") or "") or s.get("name") == SNIPPET_NAME:
            out.append({"id": s["id"], "name": s.get("name"), "active": s.get("active"),
                        "sha1": hashlib.sha1((s.get("code") or "").encode()).hexdigest(), "code": s.get("code")})
    return out


def wait_for(check, label, tries=12, pause=5):
    for i in range(tries):
        ok, info = check()
        if ok:
            return True, info
        time.sleep(pause)
    print(f"  ! {label} did not pass after {tries * pause} s: {info}")
    return False, info


def redirect_live(bust):
    def check():
        st, loc, by = source_view(bust)
        return st == 301 and (loc or "").rstrip("/") == TARGET.rstrip("/"), {"status": st, "location": loc, "x-redirect-by": by}
    return check


def bing_notify():
    try:
        from engine.util import load_credentials
        from engine.bing import working_client
        _, keys = load_credentials()
        client = working_client(keys)
        if not client:
            return "no working Bing key"
        code, text = client.submit(f"{HOME}/", [SOURCE])
        return f"HTTP {code}"
    except Exception as e:  # never fail the change over a notification
        return f"skipped ({type(e).__name__})"


CHANGELOG = Path(r"D:\Claude\SEO-Setup\05-micegoneguide\CHANGELOG-2026-10.md")
CHANGELOG_HEAD = "# micegoneguide.com changes, October 2026 (newest first)\n\n"


def log_change(entry):
    """Newest entry first, under the file's heading (created on first use)."""
    body = CHANGELOG.read_text(encoding="utf-8").removeprefix(CHANGELOG_HEAD) if CHANGELOG.exists() else ""
    CHANGELOG.write_text(CHANGELOG_HEAD + entry + "\n" + body, encoding="utf-8")
    return CHANGELOG


def utc_now():
    return dt.datetime.now(dt.timezone.utc).strftime("%H:%M UTC, %d %b %Y")


def preflight(wp):
    print("== Checks (read-only)")
    post = wp.get(f"wp/v2/posts/{POST_ID}", context="edit")
    tgt = wp.get(f"wp/v2/pages/{TARGET_ID}", context="edit", _fields="id,status,link")
    ok_t, t_info = target_ok()
    snips = snippets_mentioning(wp)
    redirecting = [s for s in snips if s["active"] and re.search(r"wp_redirect|wp_safe_redirect|Location:", s["code"] or "")]
    st_edge = source_view(False)
    print(f"  post {POST_ID}: status={post['status']} slug={post['slug']} modified_gmt={post['modified_gmt']}")
    print(f"  target page {TARGET_ID}: status={tgt['status']} live={t_info} -> {'OK' if ok_t else 'NOT OK'}")
    print(f"  old URL now (edge): {st_edge} | listed in sitemap-posts.xml: {in_sitemap()}")
    print(f"  snippets mentioning the path: {[(s['id'], s['name'], s['active']) for s in snips]}")
    problems = []
    if post["slug"] != SLUG:
        problems.append("post 928 slug changed")
    if not ok_t or tgt["status"] != "publish":
        problems.append("target /mouse-proofing/ is not a live, indexable, self-canonical page")
    if any(s["name"] == SNIPPET_NAME for s in snips):
        problems.append("the consolidation snippet already exists (already applied?)")
    if redirecting:
        problems.append(f"another active snippet already redirects this path: {[s['id'] for s in redirecting]}")
    return post, snips, problems


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--rollback")
    a = ap.parse_args()
    wp = WP(SITE)

    if a.rollback:
        bk = json.loads(Path(a.rollback).read_text(encoding="utf-8"))
        sid = bk.get("created_snippet_id")
        if sid:
            try:
                wp.post(f"code-snippets/v1/snippets/{sid}/deactivate", {})
            except SystemExit as e:
                print("  deactivate:", e)
            wp.req("DELETE", f"code-snippets/v1/snippets/{sid}", params={"force": "true"}, ok=(200, 204, 410))
            print(f"  redirect snippet {sid} removed")
        if bk["post"]["status"] == "publish":
            wp.post(f"wp/v2/posts/{POST_ID}", {"status": "publish"})
            print(f"  post {POST_ID} republished")
        print("  Cloudflare purge:", cf_purge([SOURCE] + SITEMAPS + LINKING_PAGES))
        ok, info = wait_for(lambda: (source_view(True)[0] == 200, source_view(True)), "old URL serving 200 again")
        print("  old URL:", info, "| in sitemap-posts.xml:", in_sitemap())
        log_change(f"- {utc_now()} | owner, running Claude's script | ROLLED BACK the rodent-proofing consolidation: snippet {sid} "
                   f"removed, post {POST_ID} republished | backup {Path(a.rollback).name} | old URL {info}")
        return

    post, snips, problems = preflight(wp)
    print("\nPlan: new redirect snippet (copy of #154's pattern) -> verify 301 -> post 928 to draft -> Cloudflare purge -> verify -> Bing notify -> log")
    if problems:
        sys.exit("\nSTOP: " + "; ".join(problems))
    if not a.apply:
        print("\nDry run only. Nothing was changed. Add --apply to run the plan.")
        return

    bk_path = HERE / f"backup_mgg_rodent_proofing_{stamp()}.json"
    bk = {"taken_at": dt.datetime.now().isoformat(timespec="seconds"), "post": post, "snippets_before": snips}
    bk_path.write_text(json.dumps(bk, indent=1, ensure_ascii=False), encoding="utf-8")
    print(f"\n1. backup written: {bk_path}")

    snip = wp.post("code-snippets/v1/snippets", {"name": SNIPPET_NAME, "code": SNIPPET_CODE, "scope": "front-end", "active": False})
    sid = snip["id"]
    bk["created_snippet_id"] = sid
    bk_path.write_text(json.dumps(bk, indent=1, ensure_ascii=False), encoding="utf-8")
    act = wp.post(f"code-snippets/v1/snippets/{sid}/activate", {})
    if not act.get("active"):
        wp.req("DELETE", f"code-snippets/v1/snippets/{sid}", params={"force": "true"}, ok=(200, 204, 410))
        sys.exit(f"STOP: Code Snippets did not activate the redirect snippet (code_error={act.get('code_error')}). Removed it; nothing else changed.")
    ok, info = wait_for(redirect_live(bust=True), "redirect past the cache")
    print(f"2. redirect snippet {sid} active; old URL past the cache -> {info}")
    if not ok:
        wp.post(f"code-snippets/v1/snippets/{sid}/deactivate", {})
        wp.req("DELETE", f"code-snippets/v1/snippets/{sid}", params={"force": "true"}, ok=(200, 204, 410))
        sys.exit("STOP: the redirect did not take effect. Snippet removed; the post was not touched.")

    wp.post(f"wp/v2/posts/{POST_ID}", {"status": "draft"})
    print(f"3. post {POST_ID} -> draft")

    purged = cf_purge([SOURCE] + SITEMAPS + LINKING_PAGES)
    print("4. Cloudflare purge:", purged)

    # The change itself is judged at origin; a stale edge copy is reported, not rolled back.
    ok_origin, origin = wait_for(redirect_live(bust=True), "redirect past the cache after drafting")
    ok_t, t_info = target_ok()
    if not ok_origin or not ok_t:
        print(f"   verification failed (old URL {origin}, target {t_info}) -> rolling back")
        wp.post(f"wp/v2/posts/{POST_ID}", {"status": "publish"})
        wp.post(f"code-snippets/v1/snippets/{sid}/deactivate", {})
        wp.req("DELETE", f"code-snippets/v1/snippets/{sid}", params={"force": "true"}, ok=(200, 204, 410))
        cf_purge([SOURCE] + SITEMAPS)
        sys.exit("ROLLED BACK: post republished, snippet removed. Nothing is left changed.")
    ok_edge, edge = wait_for(redirect_live(bust=False), "redirect through Cloudflare", tries=24)
    gone = not in_sitemap()
    print(f"5. old URL at origin -> {origin} | through Cloudflare -> {edge} | target {t_info} | out of sitemap-posts.xml: {gone}")
    if not ok_edge:
        print("   Cloudflare still serves its old copy" + ("" if purged else " (the purge failed)") +
              ": purge https://micegoneguide.com/rodent-proofing-for-new-constructions/ in the Cloudflare dashboard, or wait for the cache to expire.")
    if not gone:
        print("   (sitemap-posts.xml still lists it: Squirrly may cache its sitemap; re-check in an hour)")

    print("6. Bing notified of the changed URL:", bing_notify())
    p = log_change(f"- {utc_now()} | owner, running Claude's script, owner-approved 23 Oct | CONSOLIDATION: post {POST_ID} `/{SLUG}/` "
                   f"(noindex quarantine since 31 Jul; Bing 7 clicks Jul-Oct 2025, none in 2026) -> draft; new Code Snippet {sid} "
                   f"\"{SNIPPET_NAME}\" 301s the exact path to `/mouse-proofing/` (page {TARGET_ID}); Cloudflare purged; Bing notified | "
                   f"backup `{bk_path.name}`; rollback `python {Path(__file__).name} --rollback {bk_path.name}` | verified through "
                   f"Cloudflare: {edge}; target {t_info['status']} index self-canonical; out of sitemap-posts.xml: {gone}")
    print(f"7. logged in {p}")
    print(f"\nDone. Rollback: python {Path(__file__).name} --rollback {bk_path}")


if __name__ == "__main__":
    main()
