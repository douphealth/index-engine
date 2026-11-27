# -*- coding: utf-8 -*-
"""Alerts: things that changed and that the owner should know about. Recorded locally, pushed to the dashboard.

Severity: high (indexing at risk now), warn (look soon), good (a win), info.
The same URL + kind is recorded at most once a week, so a lasting problem does not flood the list.

Changes that are routine one page at a time (a page that earns nothing set to noindex, a page indexable again) are
grouped: one alert per site and kind collects every such page until it is pushed to the dashboard, so a cleanup of 80
pages is one line, not 80. On 21-27 Nov 2026 such pages made 166 of the 171 warnings.
"""
import json

from .util import log, now_iso

SEVERITY_RANK = {"info": 0, "good": 0, "warn": 1, "high": 2}
GROUP_KEEP = 200            # pages remembered per grouped alert; the count goes on beyond it


def add(db, site, url, kind, severity, message):
    dup = db.execute("SELECT 1 FROM alerts WHERE site=? AND COALESCE(url,'')=COALESCE(?, '') AND kind=? "
                     "AND julianday('now') - julianday(at) < 7", (site, url, kind)).fetchone()
    if dup:
        return False
    db.execute("INSERT INTO alerts(at, site, url, kind, severity, message) VALUES(?,?,?,?,?,?)",
               (now_iso(), site, url, kind, severity, message))
    return True


def add_to_group(db, site, kind, severity, url, detail, render):
    """Add one page to the site's open grouped alert of this kind, or open one. render(items, n) -> message.
    A page already reported in a group of this kind in the last 7 days is not counted again. Returns True when added."""
    quoted = json.dumps(url)
    seen = db.execute("SELECT 1 FROM alerts WHERE site=? AND kind=? AND url IS NULL AND instr(COALESCE(data,''), ?) > 0 "
                      "AND julianday('now') - julianday(at) < 7", (site, kind, quoted)).fetchone()
    if seen:
        return False
    row = db.execute("SELECT id, severity, data FROM alerts WHERE site=? AND kind=? AND url IS NULL AND pushed=0 "
                     "ORDER BY id DESC LIMIT 1", (site, kind)).fetchone()
    data = json.loads(row["data"]) if row and row["data"] else {"items": [], "n": 0}
    data["n"] += 1
    if len(data["items"]) < GROUP_KEEP:
        data["items"].append({"url": url, "detail": detail})
    if row:
        sev = row["severity"] if SEVERITY_RANK.get(row["severity"], 0) >= SEVERITY_RANK.get(severity, 0) else severity
        db.execute("UPDATE alerts SET severity=?, message=?, data=? WHERE id=?",
                   (sev, render(data["items"], data["n"])[:1000], json.dumps(data), row["id"]))
    else:
        db.execute("INSERT INTO alerts(at, site, url, kind, severity, message, data) VALUES(?,?,NULL,?,?,?,?)",
                   (now_iso(), site, kind, severity, render(data["items"], data["n"])[:1000], json.dumps(data)))
    return True


def group_size(db, site, kind):
    """How many pages the site's open (not yet pushed) grouped alert of this kind holds."""
    row = db.execute("SELECT data FROM alerts WHERE site=? AND kind=? AND url IS NULL AND pushed=0 ORDER BY id DESC LIMIT 1",
                     (site, kind)).fetchone()
    return json.loads(row["data"])["n"] if row and row["data"] else 0


def push(dash, db):
    rows = db.execute("SELECT * FROM alerts WHERE pushed=0 ORDER BY id LIMIT 500").fetchall()
    if not rows:
        return 0
    dash.alerts([{"engine_id": r["id"], "at": r["at"], "site": r["site"], "url": r["url"], "kind": r["kind"],
                  "severity": r["severity"], "message": r["message"]} for r in rows])
    db.executemany("UPDATE alerts SET pushed=1 WHERE id=?", [(r["id"],) for r in rows])
    db.commit()
    log(f"[alerts] pushed {len(rows)} to the dashboard")
    return len(rows)
