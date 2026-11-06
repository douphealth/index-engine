"use strict";
// The engine's state endpoint against the real D1 schema (all migrations, in order) on node:sqlite.
const test = require("node:test"), assert = require("node:assert/strict");
const fs = require("node:fs"), path = require("node:path");
const { DatabaseSync } = require("node:sqlite");

function database() {
  const sql = new DatabaseSync(":memory:");
  const dir = path.join(__dirname, "..", "migrations");
  for (const f of fs.readdirSync(dir).filter((f) => f.endsWith(".sql")).sort()) sql.exec(fs.readFileSync(path.join(dir, f), "utf8"));
  const stmt = (query) => {
    let params = [];
    const s = {
      bind(...args) { params = args; return s; },
      async all() { return { results: sql.prepare(query).all(...params) }; },
      async first() { return sql.prepare(query).get(...params) || null; },
      async run() { const r = sql.prepare(query).run(...params); return { meta: { changes: Number(r.changes) } }; },
      exec() { return /^\s*(SELECT|WITH)/i.test(query) ? { results: sql.prepare(query).all(...params) } : (sql.prepare(query).run(...params), { results: [] }); },
    };
    return s;
  };
  return { sql, DB: { prepare: stmt, async batch(list) { return list.map((s) => s.exec()); } } };
}

function proposal(db, id, site, status) {
  const t = new Date().toISOString();
  db.sql.prepare(`INSERT INTO link_proposals (id, site, source, target, anchor, status, created_at, updated_at, gain, why)
                  VALUES (?, ?, ?, ?, 'anchor words', ?, ?, ?, 1, 'test')`)
    .run(id, site, `https://${site}/source/`, `https://${site}/target-${id}/`, status, t, t);
}

async function state(db, headers = {}) {
  const { onRequestGet } = await import("../functions/api/engine/state.js");
  const res = await onRequestGet({ env: db, request: new Request("https://example.test/api/engine/state", { headers }) });
  return res.json();
}

test("link autopilot is on when never set: the engine gets proposed links, never for strict sites", async () => {
  const db = database();
  try {
    proposal(db, "a".repeat(16), "plantastichaven.com", "proposed");
    proposal(db, "b".repeat(16), "gearuptofit.com", "proposed");
    proposal(db, "c".repeat(16), "gearuptofit.com", "approved");
    const s = await state(db);
    assert.equal(s.settings.link_autopilot, true);
    assert.deepEqual(s.links.map((l) => l.id).sort(), ["a".repeat(16), "c".repeat(16)]);
  } finally { db.sql.close(); }
});

test("the owner's off switch is honoured", async () => {
  const db = database();
  try {
    db.sql.prepare("INSERT INTO settings (key, value, updated_at) VALUES ('link_autopilot', 'false', 'now')").run();
    proposal(db, "a".repeat(16), "plantastichaven.com", "proposed");
    proposal(db, "d".repeat(16), "plantastichaven.com", "approved");
    const s = await state(db);
    assert.equal(s.settings.link_autopilot, false);
    assert.deepEqual(s.links.map((l) => l.id), ["d".repeat(16)]);
  } finally { db.sql.close(); }
});

test("the heartbeat records a valid engine version and drops anything else", async () => {
  const db = database();
  try {
    await state(db, { "X-Engine-Version": "381ef37" });
    let seen = JSON.parse(db.sql.prepare("SELECT value FROM settings WHERE key='engine_last_seen'").get().value);
    assert.equal(seen.version, "381ef37");
    await state(db, { "X-Engine-Version": "<script>" });
    seen = JSON.parse(db.sql.prepare("SELECT value FROM settings WHERE key='engine_last_seen'").get().value);
    assert.equal(seen.version, null);
  } finally { db.sql.close(); }
});
