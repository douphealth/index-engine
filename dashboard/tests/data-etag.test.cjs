"use strict";
// /api/data/*: an unchanged upload is answered with an empty 304, so the browser reuses the copy it has.
const test = require("node:test"), assert = require("node:assert/strict");

function kv(entries) {
  const reads = [];
  return {
    reads,
    DATA: {
      async getWithMetadata(key) {
        const e = entries[key];
        if (!e) return { value: null, metadata: null };
        let cancelled = false;
        const value = new ReadableStream({
          start(c) { c.enqueue(new TextEncoder().encode(e.body)); c.close(); },
          cancel() { cancelled = true; },
        });
        reads.push({ key, cancelled: () => cancelled });
        return { value, metadata: e.metadata };
      },
    },
  };
}

async function get(env, path, headers = {}) {
  const { onRequestGet } = await import("../functions/api/data/[[path]].js");
  return onRequestGet({ env, params: { path }, request: new Request("https://example.test/api/data/x", { headers }) });
}

test("a dated upload is served with an ETag the browser must revalidate", async () => {
  const env = kv({ summary: { body: '{"sites":22}', metadata: { updated_at: "2026-11-26T09:15:02.123Z", gzip: false } } });
  const res = await get(env, ["summary"]);
  assert.equal(res.status, 200);
  assert.equal(res.headers.get("ETag"), '"20261126091502123"');
  assert.equal(res.headers.get("Cache-Control"), "private, no-cache");
  assert.deepEqual(await res.json(), { sites: 22 });
});

test("the same ETag gets an empty 304; a newer upload gets the data", async () => {
  const meta = { updated_at: "2026-11-26T09:15:02.123Z", gzip: false };
  const env = kv({ "site:example.com": { body: '{"urls":[1,2,3]}', metadata: meta } });
  const first = await get(env, ["site", "example.com"]);
  const etag = first.headers.get("ETag");
  const again = await get(env, ["site", "example.com"], { "If-None-Match": etag });
  assert.equal(again.status, 304);
  assert.equal(await again.text(), "");
  assert.equal(again.headers.get("ETag"), etag);
  assert.equal((await get(env, ["site", "example.com"], { "If-None-Match": `W/${etag}, "other"` })).status, 304);
  meta.updated_at = "2026-11-26T09:25:10.000Z";                       // the engine uploaded again
  const fresh = await get(env, ["site", "example.com"], { "If-None-Match": etag });
  assert.equal(fresh.status, 200);
  assert.notEqual(fresh.headers.get("ETag"), etag);
  assert.deepEqual(await fresh.json(), { urls: [1, 2, 3] });
});

test("gzipped and plain uploads never share an ETag, and gzip is passed through", async () => {
  const at = "2026-11-26T09:15:02.123Z";
  const { etagFor } = await import("../functions/api/data/[[path]].js");
  assert.notEqual(etagFor({ updated_at: at, gzip: true }), etagFor({ updated_at: at, gzip: false }));
  const env = kv({ summary: { body: "gz-bytes", metadata: { updated_at: at, gzip: true } } });
  const res = await get(env, ["summary"]);
  assert.equal(res.headers.get("Content-Encoding"), "gzip");
});

test("a value stored before uploads were dated is never cached; unknown keys are 404", async () => {
  const env = kv({ log: { body: "old log", metadata: null } });
  const res = await get(env, ["log"], { "If-None-Match": '"anything"' });
  assert.equal(res.status, 200);
  assert.equal(res.headers.get("ETag"), null);
  assert.equal(res.headers.get("Cache-Control"), "private, no-store");
  assert.equal(await res.text(), "old log");
  assert.equal((await get(env, ["health"])).status, 404);
  assert.equal((await get(env, ["site", "not a domain"])).status, 404);
  assert.equal((await get(env, ["secrets"])).status, 404);
});
