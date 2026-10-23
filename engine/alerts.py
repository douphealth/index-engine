# -*- coding: utf-8 -*-
"""Alerts: things that changed and that the owner should know about. Recorded locally, pushed to the dashboard.

Severity: high (indexing at risk now), warn (look soon), good (a win), info.
The same URL + kind is recorded at most once a week, so a lasting problem does not flood the list.
"""
from .util import log, now_iso


def add(db, site, url, kind, severity, message):
    dup = db.execute("SELECT 1 FROM alerts WHERE site=? AND COALESCE(url,'')=COALESCE(?, '') AND kind=? "
                     "AND julianday('now') - julianday(at) < 7", (site, url, kind)).fetchone()
    if dup:
        return False
    db.execute("INSERT INTO alerts(at, site, url, kind, severity, message) VALUES(?,?,?,?,?,?)",
               (now_iso(), site, url, kind, severity, message))
    return True


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
