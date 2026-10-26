# -*- coding: utf-8 -*-
"""Index Engine - find every URL your sites publish, find out why each one is or is not indexed, push the indexable
ones through every channel search engines accept, learn what gets pages indexed on YOUR sites, and act on it.
Results and controls live in the online dashboard (dashboard/).

    python indexengine.py tick                    # the scheduler, every 10 min: queued run > daily run > quick tick
    python indexengine.py run                     # everything, all enabled sites, now
    python indexengine.py run --site frenchyfab.com --dry-run
    python indexengine.py run --no-submit         # measure only
    python indexengine.py publish                 # re-analyse local data and re-upload to the dashboard (no crawling)
    python indexengine.py status                  # one line per site from the last run
    python indexengine.py selftest                # offline test with a fake site and fake APIs

Full run: [apply approved links] -> discover -> audit -> Google data + inspection -> Bing data -> submit ->
          analyse (triage, odds model, best actions, impact) -> link proposals -> report -> dashboard.
Quick tick (every 10 min): apply approved links; find posts published/updated since the last look; audit, inspect,
          gate and submit them within minutes; re-analyse and upload only when something changed.
Credentials come from ../GSC-Bing.txt and the sites' credentials.txt and never leave this PC.
"""
import argparse
import datetime as dt
import json
import os
import sys
import time
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from engine import (alerts, audit, bing, brain, config, discover, fresh, google, links, publish, report,  # noqa: E402
                    store, submit, wp)
from engine.dashboard import Dashboard, apply_state, safe  # noqa: E402
from engine.util import load_credentials, log, log_text, now_iso, open_log, parse_ts, utf8_stdout  # noqa: E402


class Locked(Exception):
    pass


class RunLock:
    """One run at a time on this PC (the 30-minute tick and a manual run must not overlap)."""

    def __enter__(self):
        f = config.LOCK_FILE
        f.parent.mkdir(parents=True, exist_ok=True)
        if f.exists() and time.time() - f.stat().st_mtime < config.LOCK_STALE_HOURS * 3600:
            raise Locked(f"another run started {time.ctime(f.stat().st_mtime)} is still going (lock {f})")
        f.write_text(json.dumps({"pid": os.getpid(), "at": now_iso()}), encoding="utf-8")
        return self

    def __exit__(self, *exc):
        config.LOCK_FILE.unlink(missing_ok=True)


def engines(args, db, sites):
    """Google and Bing clients plus each site's property / Bing site (None/{} when unavailable)."""
    sa, bing_keys = (None, []) if args.no_creds else load_credentials()
    gclient = google.GSCClient(sa) if sa and not args.no_google else None
    bclient = bing.working_client(bing_keys) if bing_keys and not args.no_bing else None
    log(f"Credentials: Google service account {'found' if sa else 'not found'}, "
        f"Bing key {'valid' if bclient else 'not found or rejected' if bing_keys else 'not found'}")
    props = google.resolve_properties(db, gclient, sites) if gclient else {}
    bsites = bing.resolve_sites(db, bclient, sites) if bclient else {}
    return gclient, bclient, props, bsites


def wp_clients(sites):
    out = {}
    for s in sites:
        if s in wp.SITE_DIRS:
            c = wp.WPClient(s)
            if c.ok:
                out[s] = c
    return out


def writes_allowed(args, settings=None):
    """No WordPress edits in measure-only, dry-run, no-credentials or no-WordPress modes."""
    return not (settings or {}).get("submit_paused") and not any(getattr(args, flag, False) for flag in
                   ("no_submit", "dry_run", "no_links", "no_creds", "no_wp"))


def apply_links(db, dash, state, settings):
    """Apply approved (or, with autopilot, auto-approved) internal links and process undo requests."""
    def report_(lid, status, result, applied_at):
        safe(dash.link_result, lid, status, result, applied_at, what="report link result")

    links.recheck(db, report_)          # applied earlier, hidden by an edge cache until now?
    disabled = set(settings.get("sites_disabled") or [])
    todo = [lk for lk in (state or {}).get("links") or []
            if lk.get("site") not in disabled and lk.get("status") in ("approved", "undo", "proposed")]
    if not todo:
        return set()
    clients = wp_clients(sorted({lk["site"] for lk in todo}))
    return links.apply(db, todo, settings, clients, report_)


def pipeline(db, args, sites):
    """The full engine. Returns (results, {site: {channel: n}}, intel, proposals or None)."""
    if not args.skip_discover:
        discover.run(db, sites, use_wp=not args.no_wp)
    if not args.skip_audit:
        audit.run(db, sites, force=args.force_audit, max_per_site=args.audit_max)

    gclient, bclient, props, bsites = engines(args, db, sites)
    if props:
        google.pull_performance(db, gclient, props)
    if bsites:
        bing.pull_performance(db, bclient, bsites)
    if props and args.inspect_max != 0:
        google.inspect_due(db, gclient, props, max_per_site=args.inspect_max, include_taxonomy=args.include_taxonomy)

    submitted = {}
    if not args.no_submit:
        submitted = submit.run(db, sites, bing_client=bclient, bing_sites=bsites, dry_run=args.dry_run, both=args.both,
                               include_taxonomy=args.include_taxonomy, max_per_site=args.submit_max)
        if props:
            google.sync_sitemaps(db, gclient, props, dry_run=args.dry_run)
    if bsites and args.bing_check:
        bing.check_url_info(db, bclient, bsites, max_per_site=args.bing_check)

    results, intel = brain.analyze(db, config.SITES)
    m = intel["model"]
    log(f"[model] trained on {m.get('n')} Google verdicts ({m.get('indexed')} indexed), held-out AUC {m.get('auc')}, "
        f"quality {m.get('quality')}; {len(intel['actions'])} best next actions")
    proposals = None
    if not args.no_links and not args.no_creds:
        proposals = links.propose(db, results, intel["link_gain"], wp_clients([s for s in sites if s in wp.SITE_DIRS]))
    path = report.write(db, results)
    log("")
    print_status(db, config.SITES)
    log(f"\nReport: {path}")
    return results, (submitted if not args.dry_run else {}), intel, proposals


def upload(dash, db, results, intel, submitted, disabled, proposals=None, proposal_sites=()):
    run_summary = safe(publish.push, dash, db, results, now_iso(), submitted, log_text(), disabled, intel, what="upload results")
    if proposals is not None:
        safe(dash.proposals, proposals, sorted(proposal_sites), what="upload link proposals")
    safe(alerts.push, dash, db, what="push alerts")
    return run_summary


def run(args, dash=None, state=None, trigger="manual"):
    with RunLock():
        db = store.connect()
        open_log("run")
        if dash is None and not args.no_dashboard:
            dash = Dashboard.from_config()
        if dash and state is None:
            state = safe(dash.state, what="pull state")
        settings = apply_state(db, state) if (dash and state) else {}

        disabled = set(settings.get("sites_disabled") or [])
        # Disabled sites are excluded even when explicitly requested from the dashboard.
        # A local CLI override would need an explicit, separately audited policy.
        sites = [s for s in (args.site or config.SITES) if s in config.SITES and s not in disabled]
        if not sites:
            log("No enabled sites selected; no indexing operations will be performed.")
            return {}
        if settings.get("submit_paused") and not args.no_submit:
            log("Submissions are paused in the dashboard settings: measuring only.")
            args.no_submit = True
        if settings.get("manual_queue_per_site") is not None:
            config.MANUAL_QUEUE_PER_SITE = int(settings["manual_queue_per_site"])
        if args.inspect_max is None and settings.get("inspect_max_per_site") is not None:
            args.inspect_max = int(settings["inspect_max_per_site"])
        options = {k: v for k, v in (("site", ",".join(args.site or [])), ("no_submit", args.no_submit),
                                     ("dry_run", args.dry_run), ("force_audit", args.force_audit)) if v}
        log(f"Index Engine run ({trigger}) - {len(sites)} sites{' (dry run: nothing is submitted)' if args.dry_run else ''}"
            f"{' | dashboard connected' if dash else ''}")

        run_id = safe(dash.run_start, trigger, options, what="open run") if dash else None
        local_id = db.execute("INSERT INTO local_runs(started_at, status, trigger) VALUES(?, 'running', ?)",
                              (now_iso(), trigger)).lastrowid
        db.commit()
        try:
            if dash and state and writes_allowed(args, settings):
                apply_links(db, dash, state, settings)
            results, submitted, intel, proposals = pipeline(db, args, sites)
        except Exception as e:
            log(traceback.format_exc())
            db.execute("UPDATE local_runs SET finished_at=?, status='failed' WHERE rowid=?", (now_iso(), local_id))
            db.commit()
            if dash:
                safe(dash.put, "log", log_text()[-200_000:], what="upload log")
                safe(dash.run_finish, run_id, "failed", error=f"{type(e).__name__}: {e}", what="close run")
            raise
        if dash:
            run_summary = upload(dash, db, results, intel, submitted, disabled, proposals,
                                 [s for s in sites if s in wp.SITE_DIRS])
            safe(dash.run_finish, run_id, "ok", summary=run_summary, what="close run")
            if run_summary:
                log(f"Dashboard updated: {dash.url}")
        db.execute("UPDATE local_runs SET finished_at=?, status='ok' WHERE rowid=?", (now_iso(), local_id))
        db.commit()
        return results


def quick(args, dash, state, settings):
    """Between daily runs: apply approved links, then catch new/updated posts and get them indexed within minutes."""
    with RunLock():
        db = store.connect()
        if state:
            apply_state(db, state)
        disabled = set(settings.get("sites_disabled") or [])
        sites = [s for s in config.SITES if s not in disabled]
        touched = apply_links(db, dash, state, settings) if (dash and state and writes_allowed(args, settings)) else set()
        changes = fresh.changed(db, sites) if settings.get("fresh_check", True) else {}
        if not changes and not touched:
            db.commit()
            print("Quick tick: nothing new.")
            return
        open_log("fresh")
        log(f"Quick tick: {sum(len(v) for v in changes.values())} new/updated URLs on {len(changes)} sites; "
            f"links applied on {len(touched)} sites")
        run_id = safe(dash.run_start, "fresh", {"site": ",".join(sorted(changes))}, what="open run") if dash else None
        local_id = db.execute("INSERT INTO local_runs(started_at, status, trigger) VALUES(?, 'running', 'fresh')",
                              (now_iso(),)).lastrowid
        submitted = {}
        if changes:
            urls = fresh.ingest(db, changes)
            gclient, bclient, props, bsites = engines(args, db, list(changes))
            if props:
                google.inspect_due(db, gclient, props, only_urls=urls)
            if not (settings.get("submit_paused") or args.no_submit or args.dry_run):
                submitted = submit.run(db, list(changes), bing_client=bclient, bing_sites=bsites)
        results, intel = brain.analyze(db, config.SITES)
        report.write(db, results)
        if dash:
            run_summary = upload(dash, db, results, intel, submitted, disabled)
            safe(dash.run_finish, run_id, "ok", summary=run_summary, what="close run")
        db.execute("UPDATE local_runs SET finished_at=?, status='ok' WHERE rowid=?", (now_iso(), local_id))
        db.commit()
        # Only advance after processing succeeds; a crash must not silently lose WordPress updates.
        if changes:
            fresh.acknowledge(db, changes)


def tick(args):
    """Scheduler entry point (every 10 minutes): heartbeat, then a queued dashboard run, else the daily run when due,
    else a quick tick."""
    if config.LOCK_FILE.exists() and time.time() - config.LOCK_FILE.stat().st_mtime < config.LOCK_STALE_HOURS * 3600:
        print("A run is in progress; nothing to do.")
        return
    dash = None if args.no_dashboard else Dashboard.from_config()
    state = safe(dash.state, what="pull state") if dash else None
    settings = (state or {}).get("settings") or {}
    cmd = next((c for c in (state or {}).get("commands", []) if c.get("kind") == "run"), None)
    if cmd:
        o = cmd.get("options") or {}
        argv = ["run"] + (["--site", o["site"]] if o.get("site") else []) + (["--no-submit"] if o.get("no_submit") else []) \
            + (["--force-audit"] if o.get("force_audit") else []) + (["--dry-run"] if o.get("dry_run") else [])
        safe(dash.command, cmd["id"], "running", what="claim command")
        try:
            run(build_parser().parse_args(argv), dash, state, trigger="dashboard")
            safe(dash.command, cmd["id"], "done", "ok", what="close command")
        except Exception as e:
            safe(dash.command, cmd["id"], "failed", f"{type(e).__name__}: {str(e)[:400]}", what="close command")
            raise
        return

    hhmm = settings.get("daily_run_time") or config.DAILY_RUN_TIME
    now = dt.datetime.now()
    due = now.replace(hour=int(hhmm[:2]), minute=int(hhmm[3:]), second=0, microsecond=0)
    db = store.connect()
    last = db.execute("SELECT MAX(started_at) FROM local_runs WHERE trigger='schedule'").fetchone()[0]
    if now >= due and not (last and parse_ts(last) >= due.astimezone(dt.timezone.utc)):
        run(build_parser().parse_args(["run"]), dash, state, trigger="schedule")
        return
    quick(args, dash, state, settings)


def print_status(db, sites):
    log(f"{'site':34s} {'URLs':>6s} {'indexed':>8s} {'crawled':>8s} {'discov.':>8s} {'unknown':>8s} {'no data':>8s} "
        f"{'conflict':>8s} {'kept out':>8s}")
    for site in sites:
        row = db.execute("SELECT data FROM snapshots WHERE site=? ORDER BY run_at DESC LIMIT 1", (site,)).fetchone()
        if not row:
            continue
        d = json.loads(row["data"])
        b = d["buckets"]
        log(f"{site:34s} {d['total']:6d} {b.get('INDEXED', 0):8d} {b.get('CRAWLED_NOT_INDEXED', 0):8d} "
            f"{b.get('DISCOVERED_NOT_INDEXED', 0):8d} {b.get('UNKNOWN_TO_GOOGLE', 0):8d} "
            f"{b.get('NOT_INSPECTED', 0) + b.get('NOT_CHECKED', 0):8d} {d.get('conflicts', 0):8d} {d.get('excluded', 0):8d}")


def build_parser():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", choices=["run", "tick", "publish", "status", "seed", "selftest"])
    ap.add_argument("--site", action="append", help="limit to a site (repeatable)")
    ap.add_argument("--dry-run", action="store_true", help="decide everything, submit and change nothing")
    ap.add_argument("--no-submit", action="store_true", help="skip the submission stage")
    ap.add_argument("--no-google", action="store_true")
    ap.add_argument("--no-bing", action="store_true")
    ap.add_argument("--no-creds", action="store_true", help="ignore the credential files entirely")
    ap.add_argument("--no-wp", action="store_true", help="do not query the WordPress REST API")
    ap.add_argument("--no-links", action="store_true", help="no link proposals and no link changes")
    ap.add_argument("--no-dashboard", action="store_true", help="do not sync with the online dashboard")
    ap.add_argument("--both", action="store_true", help="use the Bing API even where IndexNow works")
    ap.add_argument("--include-taxonomy", action="store_true", help="also inspect/submit category, tag and author archives")
    ap.add_argument("--skip-discover", action="store_true")
    ap.add_argument("--skip-audit", action="store_true")
    ap.add_argument("--force-audit", action="store_true", help="re-fetch every page even if checked recently")
    ap.add_argument("--audit-max", type=int, help="max pages to fetch per site this run")
    ap.add_argument("--inspect-max", type=int, help="max URL inspections per property this run (0 = none)")
    ap.add_argument("--submit-max", type=int, help="max URLs to submit per site per channel this run")
    ap.add_argument("--bing-check", type=int, default=200, help="GetUrlInfo checks per site this run")
    ap.add_argument("--from", dest="seed_from", default=str(config.PORTFOLIO / "portfolio-audit-2026-09-17" / "data"),
                    help="seed: folder with an export_gsc_bing.py export")
    return ap


def main():
    utf8_stdout()
    args = build_parser().parse_args()
    try:
        if args.command == "run":
            run(args)
        elif args.command == "tick":
            tick(args)
        elif args.command == "publish":
            for flag in ("skip_discover", "skip_audit", "no_submit", "no_creds", "no_links"):
                setattr(args, flag, True)
            args.inspect_max, args.bing_check = 0, 0
            run(args, trigger="publish")
        elif args.command == "status":
            print_status(store.connect(), args.site or config.SITES)
        elif args.command == "seed":
            from engine import seed
            seed.run(store.connect(), args.seed_from)
        else:
            from engine import selftest
            sys.exit(selftest.main())
    except Locked as e:
        print(e)
        sys.exit(3)


if __name__ == "__main__":
    main()
