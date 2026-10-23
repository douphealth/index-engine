# -*- coding: utf-8 -*-
"""SQLite state. Every stage reads and writes here, so runs are resumable and incremental.

All URL keys are util.norm() form (no query, no fragment)."""
import json
import sqlite3

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS urls(
  url TEXT PRIMARY KEY, site TEXT, kind TEXT, sitemap TEXT, lastmod TEXT,
  in_sitemap INT DEFAULT 0, in_wp INT DEFAULT 0, first_seen TEXT, last_seen TEXT);
CREATE INDEX IF NOT EXISTS urls_site ON urls(site);

CREATE TABLE IF NOT EXISTS audits(
  url TEXT PRIMARY KEY, checked_at TEXT, lastmod_at_check TEXT, status INT, final_url TEXT, hops INT,
  ctype TEXT, ms INT, noindex INT, noindex_src TEXT, robots_google INT, robots_bing INT,
  canonical TEXT, canonical_state TEXT, title TEXT, h1 TEXT, words INT, sketch TEXT,
  out_links INT, error TEXT);

CREATE TABLE IF NOT EXISTS links(src TEXT, dst TEXT, in_content INT, PRIMARY KEY(src, dst));
CREATE INDEX IF NOT EXISTS links_dst ON links(dst);

CREATE TABLE IF NOT EXISTS gsc(
  url TEXT PRIMARY KEY, property TEXT, inspected_at TEXT, verdict TEXT, coverage TEXT,
  indexing_state TEXT, fetch_state TEXT, robots_state TEXT, last_crawl TEXT,
  google_canonical TEXT, user_canonical TEXT, error TEXT);
CREATE TABLE IF NOT EXISTS gsc_history(url TEXT, inspected_at TEXT, coverage TEXT);

CREATE TABLE IF NOT EXISTS perf(
  url TEXT PRIMARY KEY, site TEXT, gsc_clicks INT DEFAULT 0, gsc_impr INT DEFAULT 0,
  gsc_recent_impr INT DEFAULT 0, gsc_at TEXT, bing_clicks INT DEFAULT 0, bing_impr INT DEFAULT 0, bing_at TEXT);

CREATE TABLE IF NOT EXISTS bing_info(url TEXT PRIMARY KEY, checked_at TEXT, discovered TEXT, last_crawled TEXT, http_status INT);

CREATE TABLE IF NOT EXISTS submissions(url TEXT, channel TEXT, submitted_at TEXT, lastmod TEXT, http_status INT);
CREATE INDEX IF NOT EXISTS sub_url ON submissions(url, channel);

CREATE TABLE IF NOT EXISTS quota(day TEXT, property TEXT, used INT DEFAULT 0, exhausted INT DEFAULT 0,
  PRIMARY KEY(day, property));

CREATE TABLE IF NOT EXISTS sitemap_submits(site TEXT, feed TEXT, submitted_at TEXT, url_count INT, http_status INT);
CREATE TABLE IF NOT EXISTS manual_queue(url TEXT PRIMARY KEY, queued_at TEXT);

CREATE TABLE IF NOT EXISTS triage(
  url TEXT PRIMARY KEY, site TEXT, bucket TEXT, flags TEXT, reason TEXT, action TEXT,
  value REAL, google TEXT, bing TEXT);

CREATE TABLE IF NOT EXISTS snapshots(run_at TEXT, site TEXT, data TEXT);

-- The owner's decisions from the dashboard, refreshed before every run.
CREATE TABLE IF NOT EXISTS owner_state(url TEXT PRIMARY KEY, site TEXT, ignored INT DEFAULT 0, requested_at TEXT);
-- Local run log for the scheduler (tick): one row per run.
CREATE TABLE IF NOT EXISTS local_runs(started_at TEXT, finished_at TEXT, status TEXT, trigger TEXT);
CREATE TABLE IF NOT EXISTS site_meta(site TEXT PRIMARY KEY, data TEXT);

-- Things the owner should know about (regressions, drops, gains); pushed to the dashboard once.
CREATE TABLE IF NOT EXISTS alerts(id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT, site TEXT, url TEXT, kind TEXT,
  severity TEXT, message TEXT, pushed INT DEFAULT 0);
CREATE INDEX IF NOT EXISTS alerts_dedupe ON alerts(url, kind, at);

-- Internal links the engine inserted (for verification, impact tracking and undo).
CREATE TABLE IF NOT EXISTS link_ops(id TEXT PRIMARY KEY, site TEXT, source TEXT, target TEXT, anchor TEXT, post_id INT,
  post_type TEXT, status TEXT, applied_at TEXT, backup TEXT, result TEXT);

-- The indexation-odds model's last training (coefficients, quality).
CREATE TABLE IF NOT EXISTS model(trained_at TEXT, data TEXT);
"""

# Columns added after the first release; ALTER fails harmlessly when the column exists.
MIGRATIONS = [
    "ALTER TABLE urls ADD COLUMN published TEXT",
    "ALTER TABLE audits ADD COLUMN trust TEXT",
    "ALTER TABLE triage ADD COLUMN odds REAL",
    "ALTER TABLE triage ADD COLUMN why TEXT",
]


def connect(path=None):
    path = path or config.DB_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(str(path), timeout=60)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA synchronous=NORMAL")
    db.executescript(SCHEMA)
    for sql in MIGRATIONS:
        try:
            db.execute(sql)
        except sqlite3.OperationalError:
            pass
    db.commit()
    return db


def get_meta(db, site):
    row = db.execute("SELECT data FROM site_meta WHERE site=?", (site,)).fetchone()
    return json.loads(row["data"]) if row else {}


def set_meta(db, site, **kw):
    data = get_meta(db, site)
    data.update(kw)
    db.execute("INSERT INTO site_meta(site, data) VALUES(?, ?) ON CONFLICT(site) DO UPDATE SET data=excluded.data",
               (site, json.dumps(data)))
    db.commit()
    return data


def quota_used(db, day, prop):
    row = db.execute("SELECT used, exhausted FROM quota WHERE day=? AND property=?", (day, prop)).fetchone()
    return (row["used"], bool(row["exhausted"])) if row else (0, False)


def quota_add(db, day, prop, n=1, exhausted=False):
    db.execute("INSERT INTO quota(day, property, used, exhausted) VALUES(?,?,?,?) "
               "ON CONFLICT(day, property) DO UPDATE SET used=used+excluded.used, "
               "exhausted=MAX(exhausted, excluded.exhausted)", (day, prop, n, int(exhausted)))
