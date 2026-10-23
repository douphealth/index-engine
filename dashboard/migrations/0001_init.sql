-- Index Engine dashboard: management state. Crawl results live in KV (written by the engine on each run).

CREATE TABLE IF NOT EXISTS settings (
  key        TEXT PRIMARY KEY,
  value      TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

-- What the owner decided about a URL. The engine pulls this before every run.
CREATE TABLE IF NOT EXISTS url_state (
  url          TEXT PRIMARY KEY,
  site         TEXT NOT NULL,
  ignored      INTEGER NOT NULL DEFAULT 0,
  requested_at TEXT,              -- "Request indexing" done in Search Console
  note         TEXT,
  updated_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS fixes (
  id         TEXT PRIMARY KEY,
  site       TEXT NOT NULL,
  title      TEXT NOT NULL,
  detail     TEXT,
  how        TEXT,
  status     TEXT NOT NULL DEFAULT 'open',   -- open | done | skipped
  updated_at TEXT NOT NULL
);

-- Commands from the dashboard to the engine on the owner's PC (it polls every 30 minutes).
CREATE TABLE IF NOT EXISTS commands (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  created_at  TEXT NOT NULL,
  kind        TEXT NOT NULL,                  -- run
  options     TEXT NOT NULL DEFAULT '{}',
  status      TEXT NOT NULL DEFAULT 'pending', -- pending | running | done | failed | cancelled
  picked_at   TEXT,
  finished_at TEXT,
  result      TEXT
);

CREATE TABLE IF NOT EXISTS runs (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  started_at  TEXT NOT NULL,
  finished_at TEXT,
  status      TEXT NOT NULL,                  -- running | ok | failed
  trigger     TEXT,
  options     TEXT,
  summary     TEXT,
  error       TEXT
);

CREATE TABLE IF NOT EXISTS login_attempts (
  ip TEXT NOT NULL,
  ts INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS login_attempts_ip ON login_attempts (ip, ts);
