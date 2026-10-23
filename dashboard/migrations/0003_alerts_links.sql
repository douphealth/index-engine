-- Alerts raised by the engine (regressions, Google gains and drops) and the internal-link approval queue.

CREATE TABLE IF NOT EXISTS alerts (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  engine_key TEXT UNIQUE,                 -- "<engine alert id>@<time>", so a re-push is ignored
  at         TEXT NOT NULL,
  site       TEXT,
  url        TEXT,
  kind       TEXT NOT NULL,
  severity   TEXT NOT NULL,               -- high | warn | good | info
  message    TEXT NOT NULL,
  acked      INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS alerts_open ON alerts (acked, at);

-- Contextual links the engine proposes; the owner approves; the engine on the PC applies (and undoes).
CREATE TABLE IF NOT EXISTS link_proposals (
  id           TEXT PRIMARY KEY,          -- sha1(source|target)[:16]
  site         TEXT NOT NULL,
  source       TEXT NOT NULL,
  source_title TEXT,
  target       TEXT NOT NULL,
  target_title TEXT,
  anchor       TEXT NOT NULL,             -- phrase already in the source page's text
  context      TEXT,
  score        REAL,
  gain         REAL,
  status       TEXT NOT NULL DEFAULT 'proposed',  -- proposed | approved | applied | rejected | failed | undo | undone
  created_at   TEXT NOT NULL,
  updated_at   TEXT NOT NULL,
  applied_at   TEXT,
  result       TEXT
);
CREATE INDEX IF NOT EXISTS link_proposals_status ON link_proposals (status, site);
