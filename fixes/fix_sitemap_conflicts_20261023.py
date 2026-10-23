# -*- coding: utf-8 -*-
"""Resolve the sitemap/robots conflicts the Index Engine found on 23 Oct 2026. One site per run.

    python fix_sitemap_conflicts_20261023.py --site mysticaldigits.com              # dry run: state + plan
    python fix_sitemap_conflicts_20261023.py --site mysticaldigits.com --apply      # backup, change, verify live
    python fix_sitemap_conflicts_20261023.py --site mysticaldigits.com --rollback <backup.json>

What each site gets (Bing page data checked first; nothing that earns Bing clicks is hidden):
  mysticaldigits.com       posts 1450, 6549, 7397: noindexed on purpose (25 Sep triage "T3-keep-noindex", 0 Bing clicks)
                           but still in Yoast's post sitemap, because the noindex comes from snippet #358, not Yoast.
                           -> set Yoast's own noindex flag, so Yoast drops them from the sitemap. Served robots unchanged.
  (efficientgptprompts.com /shop/ needs the same Yoast flag, set by hand in wp-admin: see PLANS.)
  frenchyfab.com           post "dental-care-tips-for-french-bulldogs" is published but 301s to /french-bulldog-dental-care/
                           (last Bing click May 2026) -> move the post to draft; the redirect stays (guardrail 2).
                           /tag/featured-post/ and /author/alexios-papaioannou-2/ are blocked by robots.txt but listed in
                           Yoast's tag and author sitemaps -> Yoast noindex on that term and that author.
No post is re-saved except the frenchyfab status change. Every write goes through wpkit.oneshot (temporary admin-only
REST route, deleted after the call) or plain REST, after a JSON backup. Credentials are never printed.
"""
import argparse
import datetime as dt
import json
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, r"D:\Claude\SEO-Setup\portfolio-audit-2026-09-17\impl")
from wpkit import WP, chain, fetch, oneshot  # noqa: E402

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
HERE = Path(__file__).resolve().parent

PLANS = {
    "mysticaldigits.com": {"noindex_posts": [1450, 6549, 7397], "sitemaps": ["/post-sitemap.xml"]},
    # efficientgptprompts.com is not here: Code Snippets there does not activate the temporary route (23 Oct).
    # Same fix by hand: wp-admin > Pages > Shop > Yoast box > Advanced > "Allow search engines to show this Page" = No.
    "frenchyfab.com": {"draft_slug": "dental-care-tips-for-french-bulldogs", "redirect_to": "https://frenchyfab.com/french-bulldog-dental-care/",
                       "noindex_term": ("post_tag", "featured-post"), "noindex_author": "alexios-papaioannou-2",
                       "sitemaps": ["/post-sitemap.xml", "/post_tag-sitemap.xml", "/author-sitemap.xml"]},
}

PHP_STATE = r"""
$out = array( 'posts' => array() );
foreach ( array_map( 'intval', (array) $req->get_param( 'posts' ) ) as $id ) {
  $p = get_post( $id );
  $row = array( 'status' => $p ? $p->post_status : null, 'type' => $p ? $p->post_type : null, 'slug' => $p ? $p->post_name : null,
                'noindex_meta' => get_post_meta( $id, '_yoast_wpseo_meta-robots-noindex', true ),
                'modified_gmt' => $p ? $p->post_modified_gmt : null, 'link' => get_permalink( $id ) );
  $out['posts'][ (string) $id ] = $row;
}
if ( $req->get_param( 'shop' ) && function_exists( 'wc_get_page_id' ) ) {
  $sid = (int) wc_get_page_id( 'shop' );
  $out['shop'] = array( 'id' => $sid, 'link' => get_permalink( $sid ), 'noindex_meta' => get_post_meta( $sid, '_yoast_wpseo_meta-robots-noindex', true ) );
}
$tax = (string) $req->get_param( 'tax' ); $slug = (string) $req->get_param( 'term' );
if ( $tax && $slug ) {
  $t = get_term_by( 'slug', $slug, $tax );
  $out['term'] = $t ? array( 'id' => $t->term_id, 'count' => $t->count, 'link' => get_term_link( $t ),
    'noindex' => class_exists( 'WPSEO_Taxonomy_Meta' ) ? WPSEO_Taxonomy_Meta::get_term_meta( $t, $tax, 'noindex' ) : 'n/a' ) : null;
}
$au = (string) $req->get_param( 'author' );
if ( $au ) {
  $u = get_user_by( 'slug', $au );
  $out['author'] = $u ? array( 'id' => $u->ID, 'posts' => count_user_posts( $u->ID ), 'link' => get_author_posts_url( $u->ID ),
    'noindex' => get_user_meta( $u->ID, 'wpseo_noindex_author', true ) ) : null;
}
return $out;
"""

# Writes only the IDs this run names; allow-lists are passed per site and re-checked here.
PHP_CHANGE = r"""
$allowed_posts = array( 1450, 6549, 7397 );
$shop_ok       = (int) $req->get_param( 'shop_id' );
$key   = '_yoast_wpseo_meta-robots-noindex';
$res   = array( 'posts' => array() );
$build = null; $repo = null;
try {
  if ( function_exists( 'YoastSEO' ) ) {
    $build = YoastSEO()->classes->get( \Yoast\WP\SEO\Builders\Indexable_Builder::class );
    $repo  = YoastSEO()->classes->get( \Yoast\WP\SEO\Repositories\Indexable_Repository::class );
  }
} catch ( \Throwable $e ) {}
$rebuild = function ( $id, $type ) use ( $build, $repo ) {
  if ( ! $build || ! $repo ) { return 'no yoast'; }
  try {
    $ind = $repo->find_by_id_and_type( (int) $id, $type, false );          // rebuild the existing row, never add a second one
    $build->build_for_id_and_type( (int) $id, $type, $ind ? $ind : false );
    return 'ok';
  } catch ( \Throwable $e ) { return 'error: ' . $e->getMessage(); }
};
$posts = (array) $req->get_param( 'post_meta' );          // id => '1' (apply) or previous value (rollback, '' = delete)
foreach ( $posts as $id => $val ) {
  $id = (int) $id;
  if ( ! in_array( $id, $allowed_posts, true ) && $id !== $shop_ok ) { $res['posts'][ $id ] = 'not allowed'; continue; }
  $before = get_post_meta( $id, $key, true );
  if ( '' === (string) $val ) { delete_post_meta( $id, $key ); } else { update_post_meta( $id, $key, (string) $val ); }
  clean_post_cache( $id );
  if ( has_action( 'litespeed_purge_post' ) ) { do_action( 'litespeed_purge_post', $id ); }
  $res['posts'][ $id ] = array( 'before' => $before, 'after' => get_post_meta( $id, $key, true ), 'indexable' => $rebuild( $id, 'post' ) );
}
$term = $req->get_param( 'term' );                        // array( tax, id, value )
if ( is_array( $term ) && in_array( $term['tax'], array( 'post_tag' ), true ) && class_exists( 'WPSEO_Taxonomy_Meta' ) ) {
  $tid = (int) $term['id'];
  $t = get_term( $tid, $term['tax'] );
  if ( $t && 'featured-post' === $t->slug ) {
    $before = WPSEO_Taxonomy_Meta::get_term_meta( $t, $term['tax'], 'noindex' );
    WPSEO_Taxonomy_Meta::set_value( $tid, $term['tax'], 'noindex', (string) $term['value'] );
    $res['term'] = array( 'before' => $before, 'after' => WPSEO_Taxonomy_Meta::get_term_meta( $tid, $term['tax'], 'noindex' ), 'indexable' => $rebuild( $tid, 'term' ) );
  }
}
$author = $req->get_param( 'author' );                    // array( id, value )
if ( is_array( $author ) ) {
  $u = get_user_by( 'id', (int) $author['id'] );
  if ( $u && 'alexios-papaioannou-2' === $u->user_nicename ) {
    $before = get_user_meta( $u->ID, 'wpseo_noindex_author', true );
    if ( '' === (string) $author['value'] ) { delete_user_meta( $u->ID, 'wpseo_noindex_author' ); } else { update_user_meta( $u->ID, 'wpseo_noindex_author', (string) $author['value'] ); }
    $res['author'] = array( 'before' => $before, 'after' => get_user_meta( $u->ID, 'wpseo_noindex_author', true ), 'indexable' => $rebuild( $u->ID, 'user' ) );
  }
}
if ( class_exists( 'WPSEO_Sitemaps_Cache' ) ) { WPSEO_Sitemaps_Cache::clear(); $res['sitemap_cache'] = 'cleared'; }
delete_transient( 'wpseo_sitemap_cache_validator' );
$paths = array_map( 'strval', (array) $req->get_param( 'purge_paths' ) );
if ( $paths ) { header( 'X-LiteSpeed-Purge: ' . implode( ', ', $paths ), false ); $res['purged'] = $paths; }
return $res;
"""


def stamp():
    return dt.datetime.now().strftime("%Y%m%d-%H%M%S")


def cleanup(wp):
    """wpkit.oneshot's own delete treats Code Snippets' 204 reply as a failure and can leave its inactive temp
    snippet behind. Remove every temp snippet this script created (matched by name), nothing else."""
    for s in wp.get("code-snippets/v1/snippets"):
        name = s.get("name") or ""
        if name.startswith("PGCC TEMP oneshot conflict-") and not s.get("active"):
            wp.req("DELETE", f"code-snippets/v1/snippets/{s['id']}", params={"force": "true"}, ok=(200, 204, 410))
            print(f"  removed temp snippet {s['id']}")


def served(url):
    """robots meta (all of them) and status, cache-busted and plain."""
    out = {}
    for label, bust in (("fresh", True), ("edge", False)):
        r = fetch(url, allow_redirects=False, bust=bust)
        metas = re.findall(r"<meta[^>]+name=['\"]robots['\"][^>]*content=['\"]([^'\"]*)", r.text, re.I)
        out[label] = {"status": r.status_code, "robots": metas or ["(none)"], "location": r.headers.get("location")}
    return out


def sitemap_lists(site, path, url):
    r = fetch(f"https://{site}{path}")
    return {"status": r.status_code, "listed": url in r.text}


def read_state(wp, site, plan):
    params = {"posts[]": plan.get("noindex_posts", [])}
    if plan.get("noindex_shop"):
        params["shop"] = 1
    if plan.get("noindex_term"):
        params["tax"], params["term"] = plan["noindex_term"]
    if plan.get("noindex_author"):
        params["author"] = plan["noindex_author"]
    st = oneshot(wp, f"conflict-state-{site}", PHP_STATE, "conflict-state", method="GET", params=params)
    if not isinstance(st.get("posts"), dict):
        st["posts"] = {}                          # PHP encodes an empty array as [] rather than {}
    if plan.get("draft_slug"):
        found = wp.get("wp/v2/posts", slug=plan["draft_slug"], status="publish,draft", context="edit", _fields="id,status,link,slug")
        st["draft_post"] = found[0] if found else None
    return st


def targets(site, plan, st):
    """(label, url, sitemap path) for every URL this run changes."""
    out = []
    for pid, row in st.get("posts", {}).items():
        out.append((f"post {pid}", row["link"], plan["sitemaps"][0]))
    if st.get("shop"):
        out.append((f"shop page {st['shop']['id']}", st["shop"]["link"], "/product-sitemap.xml"))
    if st.get("draft_post"):
        out.append((f"post {st['draft_post']['id']}", st["draft_post"]["link"], "/post-sitemap.xml"))
    if st.get("term"):
        out.append((f"tag {st['term']['id']}", st["term"]["link"], "/post_tag-sitemap.xml"))
    if st.get("author"):
        out.append((f"author {st['author']['id']}", st["author"]["link"], "/author-sitemap.xml"))
    return out


def report(site, plan, st, title, urls_from=None):
    """urls_from: the state captured before the change (a drafted post's link turns into ?p=ID afterwards)."""
    print(f"\n== {title}")
    print("  WordPress/Yoast state:", json.dumps(st, ensure_ascii=False))
    for label, url, sm in targets(site, plan, urls_from or st):
        s = served(url)
        print(f"  {label:<14} {url}\n      served fresh={s['fresh']['status']} {s['fresh']['robots']} -> {s['fresh']['location'] or ''}"
              f" | edge={s['edge']['status']} {s['edge']['robots']} | in {sm}: {sitemap_lists(site, sm, url)}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--site", required=True, choices=sorted(PLANS))
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--rollback")
    a = ap.parse_args()
    wp = WP(a.site)
    try:
        run(wp, a)
    finally:
        cleanup(wp)


def run(wp, a):
    site, plan = a.site, PLANS[a.site]
    if a.rollback:
        bk = json.loads(Path(a.rollback).read_text(encoding="utf-8"))
        st = bk["state"]
        params = {"purge_paths[]": plan["sitemaps"]}
        for pid, row in st.get("posts", {}).items():
            params[f"post_meta[{pid}]"] = row.get("noindex_meta") or ""
        if st.get("shop"):
            params[f"post_meta[{st['shop']['id']}]"] = st["shop"].get("noindex_meta") or ""
            params["shop_id"] = st["shop"]["id"]
        if st.get("term"):
            params.update({"term[tax]": plan["noindex_term"][0], "term[id]": st["term"]["id"], "term[value]": st["term"]["noindex"] or "default"})
        if st.get("author"):
            params.update({"author[id]": st["author"]["id"], "author[value]": st["author"]["noindex"] or ""})
        print(json.dumps(oneshot(wp, f"conflict-rollback-{site}", PHP_CHANGE, "conflict-change", method="POST", params=params), indent=1))
        if st.get("draft_post") and st["draft_post"]["status"] == "publish":
            wp.post(f"wp/v2/posts/{st['draft_post']['id']}", {"status": "publish"})
            print("  post", st["draft_post"]["id"], "republished")
        time.sleep(3)
        report(site, plan, read_state(wp, site, plan), "State after rollback", urls_from=st)
        return

    st = read_state(wp, site, plan)
    report(site, plan, st, "Current state (nothing changed yet)")
    if st.get("shop") and not str(st["shop"]["link"]).rstrip("/").endswith("/shop"):
        sys.exit("STOP: the WooCommerce shop page is not /shop/; refusing to guess.")
    if st.get("draft_post") and st["draft_post"]["status"] != "publish":
        print("  (the post is already not published)")
    if not a.apply:
        print("\nDry run only. Nothing was changed. Add --apply to make the changes listed in the docstring.")
        return

    bk_path = HERE / f"backup_{site}_{stamp()}.json"
    bk_path.write_text(json.dumps({"taken_at": dt.datetime.now().isoformat(timespec="seconds"), "site": site, "state": st},
                                  indent=1, ensure_ascii=False), encoding="utf-8")
    print(f"\n  backup written: {bk_path}")

    params = {"purge_paths[]": plan["sitemaps"]}
    for pid in st.get("posts", {}):
        params[f"post_meta[{pid}]"] = "1"
    if st.get("shop"):
        params[f"post_meta[{st['shop']['id']}]"] = "1"
        params["shop_id"] = st["shop"]["id"]
    if st.get("term"):
        params.update({"term[tax]": plan["noindex_term"][0], "term[id]": st["term"]["id"], "term[value]": "noindex"})
    if st.get("author"):
        params.update({"author[id]": st["author"]["id"], "author[value]": "on"})
    if len(params) > 1:
        print("  Yoast changes:", json.dumps(oneshot(wp, f"conflict-change-{site}", PHP_CHANGE, "conflict-change", method="POST", params=params)))
    if st.get("draft_post") and st["draft_post"]["status"] == "publish":
        wp.post(f"wp/v2/posts/{st['draft_post']['id']}", {"status": "draft"})
        hops = chain(st["draft_post"]["link"])
        print("  post", st["draft_post"]["id"], "-> draft; old URL now:", hops)
        if not hops or hops[0][0] not in (301, 308) or hops[-1][1].rstrip("/") != plan["redirect_to"].rstrip("/"):
            wp.post(f"wp/v2/posts/{st['draft_post']['id']}", {"status": "publish"})
            sys.exit("STOP: after drafting, the old URL no longer 301s to its target. Post republished; nothing else to undo for it.")
    time.sleep(5)
    report(site, plan, read_state(wp, site, plan), "State after the change", urls_from=st)
    print(f"\nRollback: python {Path(__file__).name} --site {site} --rollback {bk_path}")


if __name__ == "__main__":
    main()
