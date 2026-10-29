# -*- coding: utf-8 -*-
"""Read-only connection diagnostics. Never copy credentials, access tokens or API responses to the dashboard.

Checks are performed by the existing local engine, not by a public Cloudflare Worker.
A configured file is NOT proof of authorization. All errors are categorical so secrets
and remote response bodies cannot enter logs, GitHub or KV.
"""
import json
from pathlib import Path
from urllib.parse import urlsplit

from . import bing, config, google, indexnow, store, wp
from .dashboard import Dashboard
from .util import load_credentials, now_iso


def item(status, detail):
    return {"status": status, "detail": detail}


def matched_property(site, entries):
    dom = site.split("://")[-1].rstrip("/")
    possibilities = (
        "sc-domain:" + dom, "https://" + dom + "/", "https://www." + dom + "/",
        "http://" + dom + "/", "http://www." + dom + "/"
    )
    return next((p for p in possibilities
                 if any(e.get("siteUrl") == p and
                        e.get("permissionLevel") not in (None, "siteUnverifiedUser")
                        for e in entries)), None)


def matched_bing(site, entries):
    host = site.split("://")[-1].rstrip("/").removeprefix("www.").lower()
    for entry in entries:
        url = entry.get("Url", "")
        eh = urlsplit(url).hostname or ""
        eh = eh.removeprefix("www.").lower()
        if eh == host and entry.get("IsVerified") is not False:
            return url
    return None


def collect(online=True, dash=None, check_indexnow=True):
    """Collect evidence with read-only operations; return only categorical status and site names."""
    report = {"version": 1, "checked_at": now_iso(), "mode": "online" if online else "offline",
              "sources": {}, "sites": [], "privacy": "No credential values or remote response bodies."}
    source = report["sources"]
    sa, keys = load_credentials()
    source["credential_file"] = item("configured" if config.CRED_FILE.is_file() else "missing",
                                     "Local credential file found" if config.CRED_FILE.is_file() else
                                     "Local credential file missing from the configured path")
    gsc = None
    bing_entries = None
    if not sa:
        source["google"] = item("missing", "Google service-account JSON was not found or could not be parsed")
    elif not online:
        source["google"] = item("unverified", "Service-account JSON parsed; API authorization not tested")
    else:
        try:
            gsc = google.GSCClient(sa).list_sites()
            source["google"] = item("ok", "Search Console sites.list succeeded")
        except Exception:
            source["google"] = item("error", "Google authentication or Search Console API access failed")
    if not keys:
        source["bing"] = item("missing", "No Bing API key candidate was found")
    elif not online:
        source["bing"] = item("unverified", "Bing API key candidate found; authorization not tested")
    else:
        for key in keys:
            try:
                bing_entries = bing.BingClient(key).user_sites()
                source["bing"] = item("ok", "Bing GetUserSites succeeded")
                break
            except Exception:
                continue
        if bing_entries is None:
            source["bing"] = item("error", "Bing rejected every available key or its API was unreachable")

    if not dash:
        source["dashboard_engine_token"] = item("missing", "Local Worker URL and engine token are not configured")
    elif not online:
        source["dashboard_engine_token"] = item("configured", "Engine token configured; Worker authorization not checked")
    else:
        try:
            if not isinstance(dash.state(), dict):
                raise ValueError("invalid response")
            source["dashboard_engine_token"] = item("ok", "Worker accepted engine token and returned state")
        except Exception:
            source["dashboard_engine_token"] = item("error", "Worker authorization or read-back failed")

    source["cloudflare_cache_purge"] = item(
        "configured" if config.CF_TOKEN_FILE.is_file() else "missing",
        "Token file present; Cache Purge permission is not independently verified" if config.CF_TOKEN_FILE.is_file()
        else "Local cache-purge token file missing")
    source["windows_scheduler"] = item("unverified", "Task Scheduler cannot be verified from a Cloudflare deployment")
    for site in config.SITES:
        row = {"site": site}
        if gsc is None:
            row["google"] = "unverified" if sa and not online else source["google"]["status"]
        else:
            row["google"] = "ok" if matched_property(site, gsc) else "missing"
        if bing_entries is None:
            row["bing"] = "unverified" if keys and not online else source["bing"]["status"]
        else:
            row["bing"] = "ok" if matched_bing(site, bing_entries) else "missing"
        if site not in wp.SITE_DIRS:
            row["wordpress"] = "not_applicable"
        else:
            path = config.PORTFOLIO / wp.SITE_DIRS[site] / "credentials.txt"
            # Presence is not proof of valid WP application password or edit permission.
            row["wordpress"] = "configured" if path.is_file() else "missing"
        if not online or not check_indexnow:
            row["indexnow"] = "unverified"
        else:
            try:
                # Read-only diagnostics must not create or alter the local SQLite state.
                # Key discovery needs only public key files and site metadata, so use
                # an existing database in read-only mode when available.
                import sqlite3
                if not config.DB_PATH.is_file():
                    raise FileNotFoundError("local engine database unavailable")
                db = sqlite3.connect(f"file:{config.DB_PATH.as_posix()}?mode=ro", uri=True)
                db.row_factory = sqlite3.Row
                try:
                    result = indexnow.detect_key(db, site, persist=False)
                finally:
                    db.close()
                row["indexnow"] = "ok" if result else "missing"
            except Exception:
                row["indexnow"] = "unverified"
        report["sites"].append(row)
    return report


def print_report(report):
    """Human-readable, sanitized. Never prints values or paths from secret files."""
    for name, info in report["sources"].items():
        print(f"{name}: {info['status']} - {info['detail']}")
    print("site | Google | Bing | IndexNow | WordPress")
    for row in report["sites"]:
        print(f"{row['site']} | {row['google']} | {row['bing']} | {row['indexnow']} | {row['wordpress']}")
    return report


def run(online=True, publish=True):
    dash = Dashboard.from_config() if publish else None
    report = collect(online=online, dash=dash)
    uploaded = False
    if dash:
        try:
            result = dash.put("health", report)
            uploaded = bool(result and result.get("ok"))
        except Exception:
            pass
    print_report(report)
    print("Dashboard health snapshot: " + ("uploaded" if uploaded else "not uploaded (local-only result)"))
    report["uploaded"] = uploaded
    return report
