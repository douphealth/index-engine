# -*- coding: utf-8 -*-
"""Index Engine - find every URL your sites publish, find out why each one is or is not indexed,
push the indexable ones through every channel search engines accept, and tell you exactly what
to fix for the rest. Results and controls live in the online dashboard (dashboard/).

    python indexengine.py tick                    # what the scheduler runs every 30 min: queued run, else the daily run
    python indexengine.py run                     # everything, all enabled sites, now
    python indexengine.py run --site frenchyfab.com --dry-run
    python indexengine.py run --no-submit         # measure only
    python indexengine.py publish                 # re-upload the latest results to the dashboard (no crawling)
    python indexengine.py status                  # one line per site from the last run
    python indexengine.py selftest                # offline test with a fake site and fake APIs

Stages: discover -> audit -> Google data + inspection -> Bing data -> submit -> triage -> report -> dashboard.
Credentials come from ../GSC-Bing.txt and never leave this PC; the dashboard only receives results.
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

from engine import audit, bing, config, discover, google, publish, report, store, submit, triage  # noqa: E402
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


def pipeline(db, args, sites):
    """The engine itself. Returns (triage results, {site: {channel: n_submitted}})."""
    if not args.skip_discover:
        discover.run(db, sites, use_wp=not args.no_wp)
    if not args.skip_audit:
        audit.run(db, sites, force=args.force_audit, max_per_site=args.audit_max)

    sa, bing_keys = (None, []) if args.no_creds else load_credentials()
    gclient = google.GSCClient(sa) if sa and not args.no_google else None
    bclient = bing.working_client(bing_keys) if bing_keys and not args.no_bing else None
    log(f"Credentials: Google service account {'found' if sa else 'not found'}, "
        f"Bing key {'valid' if bclient else 'not found or rejected' if bing_keys else 'not found'}")
    props = google.resolve_properties(db, gclient, sites) if gclient else {}
    bsites = bing.resolve_sites(db, bclient, sites) if bclient else {}
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

    results = triage.run(db, sites)
    path = report.write(db, results)
    log("")
    print_status(db, sites)
    log(f"\nReport: {path}")
    return results, submitted if not args.dry_run else {}


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
        sites = args.site or [s for s in config.SITES if s not in disabled]
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
        cur = db.execute("INSERT INTO local_runs(started_at, status, trigger) VALUES(?, 'running', ?)", (now_iso(), trigger))
        local_id = cur.lastrowid
        db.commit()
        try:
            results, submitted = pipeline(db, args, sites)
        except Exception as e:
            log(traceback.format_exc())
            db.execute("UPDATE local_runs SET finished_at=?, status='failed' WHERE rowid=?", (now_iso(), local_id))
            db.commit()
            if dash:
                safe(dash.put, "log", log_text()[-200_000:], what="upload log")
                safe(dash.run_finish, run_id, "failed", error=f"{type(e).__name__}: {e}", what="close run")
            raise
        if dash:
            # The dashboard always shows every site: re-triage the ones this run skipped from local data (no network).
            rest = [s for s in config.SITES if s not in results]
            if rest:
                results.update(triage.run(db, rest))
            run_summary = safe(publish.push, dash, db, results, now_iso(), submitted, log_text(), disabled,
                               what="upload results")
            safe(dash.run_finish, run_id, "ok", summary=run_summary, what="close run")
            if run_summary:
                log(f"Dashboard updated: {dash.url}")
        db.execute("UPDATE local_runs SET finished_at=?, status='ok' WHERE rowid=?", (now_iso(), local_id))
        db.commit()
        return results


def tick(args):
    """Scheduler entry point (every 30 minutes). Heartbeat, then a queued dashboard run, else the daily run when due."""
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
    if now < due:
        print(f"Daily run is due at {hhmm}; nothing to do.")
        return
    db = store.connect()
    last = db.execute("SELECT MAX(started_at) FROM local_runs WHERE trigger='schedule'").fetchone()[0]
    if last and parse_ts(last) >= due.astimezone(dt.timezone.utc):
        print(f"Today's scheduled run already started at {last}; nothing to do.")
        return
    run(build_parser().parse_args(["run"]), dash, state, trigger="schedule")


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
    ap.add_argument("--dry-run", action="store_true", help="decide everything, submit nothing")
    ap.add_argument("--no-submit", action="store_true", help="skip the submission stage")
    ap.add_argument("--no-google", action="store_true")
    ap.add_argument("--no-bing", action="store_true")
    ap.add_argument("--no-creds", action="store_true", help="ignore the credential file entirely")
    ap.add_argument("--no-wp", action="store_true", help="do not query the WordPress REST API")
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
            for flag in ("skip_discover", "skip_audit", "no_submit", "no_creds"):
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
