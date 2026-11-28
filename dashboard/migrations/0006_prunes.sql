-- The prune queue: the prune plan's clear-cut "noindex" and "redirect" decisions (engine/prune.py), one row per page.
-- Nothing is applied until the owner approves a row; the engine sets the rule through the site's Index Engine helper,
-- verifies it on the live page, and undoes it on request. There is no autopilot for this queue.
CREATE TABLE IF NOT EXISTS prune_proposals (
  id          TEXT PRIMARY KEY,           -- sha1("prune|" + url)[:16]
  site        TEXT NOT NULL,
  url         TEXT NOT NULL,
  title       TEXT,
  action      TEXT NOT NULL,              -- noindex | redirect
  target      TEXT,                       -- redirect target
  why         TEXT,
  evidence    TEXT,                       -- JSON {bucket, flags, google_impr_90d, bing_impr, words, age_days, inbound_links}
  status      TEXT NOT NULL DEFAULT 'proposed',  -- proposed | approved | applied | rejected | failed | undo | undone
  created_at  TEXT NOT NULL,
  updated_at  TEXT NOT NULL,
  applied_at  TEXT,
  result      TEXT
);
CREATE INDEX IF NOT EXISTS prune_proposals_status ON prune_proposals (status, site);
