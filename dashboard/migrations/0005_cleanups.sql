-- The cleanup queue: automated-content scaffolding (numbered template sections, pipeline leftovers, boilerplate blocks
-- repeated across posts) the engine proposes to cut, one row per post. The owner approves; the engine on the PC applies
-- (with backup and verification) and undoes on request. There is no autopilot for cleanups.
CREATE TABLE IF NOT EXISTS cleanup_proposals (
  id          TEXT PRIMARY KEY,           -- sha1("cleanup|" + url)[:16]
  site        TEXT NOT NULL,
  url         TEXT NOT NULL,
  title       TEXT,
  sections    TEXT NOT NULL,              -- JSON [{heading, kind, words, excerpt}]
  words       INTEGER NOT NULL DEFAULT 0,
  status      TEXT NOT NULL DEFAULT 'proposed',  -- proposed | approved | applied | rejected | failed | undo | undone
  created_at  TEXT NOT NULL,
  updated_at  TEXT NOT NULL,
  applied_at  TEXT,
  result      TEXT
);
CREATE INDEX IF NOT EXISTS cleanup_proposals_status ON cleanup_proposals (status, site);
