"use strict";
const test = require("node:test"), assert = require("node:assert/strict");
const fs = require("node:fs"), vm = require("node:vm"), path = require("node:path");
vm.runInThisContext(fs.readFileSync(path.join(__dirname, "../public/workspace-model.js"), "utf8"));
const M = globalThis.IndexWorkspace;
const NOW = Date.parse("2026-10-30T10:00:00Z");
const fixture = () => {
  const cols = ["url", "bucket", "title", "flags", "checked_at", "inspected_at", "google_verdict", "google_error", "gsc_clicks", "bing_clicks", "linked_at"];
  const row = (slug, bucket, extra = {}) => cols.map(k => ({ url: "https://one.example/" + slug, bucket, title: slug, flags: "", checked_at: "2026-10-30T09:00:00Z", inspected_at: "2026-10-30T08:00:00Z", google_verdict: "NEUTRAL", gsc_clicks: 0, bing_clicks: 0, ...extra })[k] ?? "");
  return { summary: { sites: [{ site: "one.example", gsc_property: "sc-domain:one.example" }, { site: "two.example" }] }, state: { settings: {}, url_state: [] }, tables: {
    "one.example": { site: "one.example", cols, generated_at: "2026-10-30T09:30:00Z", rows: [row("indexed", "INDEXED", { google_verdict: "PASS" }), row("missing", "BROKEN", { gsc_clicks: 50 }), row("timeout", "FETCH_FAILED"), row("private", "NOINDEX"), row("unknown", "NOT_INSPECTED", { inspected_at: "" })] }
  } };
};
const build = f => M.build(f.summary, f.state, f.tables, NOW);
test("uses technical priority before historical value, with explicit missing coverage", () => { const d = build(fixture()); assert.equal(d.items[0].bucket, "FETCH_FAILED"); assert.equal(d.items[1].url, "https://one.example/missing"); assert.equal(d.coverage.filter(s => s.loaded).length, 1); });
test("does not call missing Google evidence a non-indexed verdict", () => { const r = build(fixture()).items.find(r => r.bucket === "NOT_INSPECTED"); assert.equal(r.group, "evidence"); assert.match(r.next, /missing data does not mean not indexed/); });
test("exclusions are review items, not automatic removal instructions", () => { const r = build(fixture()).items.find(r => r.bucket === "NOINDEX"); assert.equal(r.group, "exclusions"); assert.match(r.next, /before removing/); });
test("healthy indexed pages leave default work but remain in all URLs", () => { const { items } = build(fixture()); assert.equal(M.select(items).length, 4); assert.equal(M.select(items, { mode: "all" }).length, 5); });
test("paused sites are excluded by default", () => { const f = fixture(); f.state.settings.sites_disabled = { value: ["one.example"] }; const { items } = build(f); assert.equal(M.select(items).length, 0); assert.equal(M.select(items, { includePaused: true }).length, 4); });
test("ignore is reversible immediately even when an older snapshot still contains IGNORED", () => { const f = fixture(); f.tables["one.example"].rows[1][3] = "IGNORED"; f.state.url_state = [{ url: "https://one.example/missing", ignored: 0 }]; assert.equal(build(f).items.find(x => x.bucket === "BROKEN").ignored, false); });
test("ignored URLs stay excluded without falsifying their recorded bucket", () => { const f = fixture(); f.state.url_state = [{ url: "https://one.example/missing", ignored: 1 }]; const d = build(f); assert.equal(M.select(d.items).length, 3); assert.equal(d.items.find(x => x.ignored).bucket, "BROKEN"); });
test("rejects URL credentials and foreign hosts", () => { for (const u of ["javascript:alert(1)", "https://one.example.evil.test/a", "https://user:pass@one.example/", "data:text/html,x"]) assert.equal(M.pageURL(u, "one.example"), null); assert.equal(M.pageURL("https://www.one.example/a", "one.example"), "https://www.one.example/a"); });
test("skips foreign and invalid rows and reports incomplete records", () => { const f = fixture(); f.tables["one.example"].rows.push(["https://evil.example/a", "BROKEN"], null); const d = build(f); assert.equal(d.items.length, 5); assert.equal(d.coverage[0].invalid, 2); });
test("ignores a mismatched site snapshot", () => { const f = fixture(); f.tables["one.example"].site = "wrong.example"; assert.equal(build(f).items.length, 0); });
test("deduplicates duplicate URL records", () => { const f = fixture(); f.tables["one.example"].rows.push(f.tables["one.example"].rows[0]); assert.equal(build(f).items.length, 5); });
test("old exporters remain usable but never pretend to supply fresh evidence", () => { const f = fixture(); f.tables["one.example"] = { site: "one.example", cols: ["url", "bucket"], rows: [["https://one.example/a", "INDEXED"]] }; const r = build(f).items[0]; assert.equal(r.stale, true); assert.equal(r.clicksKnown, false); });
test("fresh upload does not make an old crawl fresh", () => { const f = fixture(); f.tables["one.example"].rows[0][4] = "2026-08-01T00:00:00Z"; assert.equal(build(f).items.find(x => x.bucket === "INDEXED").stale, true); });
test("future and invalid evidence timestamps are not trusted", () => { assert.equal(M.age("garbage", NOW), null); assert.equal(M.age("2027-01-01T00:00:00Z", NOW), null); });
test("manual request is not proof of indexing", () => { const v = M.verification({ bucket: "INDEXED", inspected_at: "2026-10-29T08:00:00Z", google_verdict: "PASS" }, { requested_at: "2026-10-30T09:00:00Z" }, NOW); assert.equal(v.key, "awaiting"); });
test("post-action successful inspection is observed, not causal", () => { const v = M.verification({ inspected_at: "2026-10-30T09:00:00Z", google_verdict: "PASS" }, { requested_at: "2026-10-29T08:00:00Z" }, NOW); assert.equal(v.key, "observed"); assert.match(v.label, /causation unproven/); });
test("failed inspection cannot certify follow-up", () => { const v = M.verification({ inspected_at: "2026-10-30T09:00:00Z", google_verdict: "PASS", google_error: "503" }, { requested_at: "2026-10-29T08:00:00Z" }, NOW); assert.equal(v.key, "stale"); });
test("latest link action takes precedence over older request", () => { const v = M.verification({ linked_at: "2026-10-30T09:00:00Z", inspected_at: "2026-10-30T08:00:00Z", google_verdict: "PASS" }, { requested_at: "2026-10-28T08:00:00Z" }, NOW); assert.equal(v.key, "awaiting"); });
test("inspection after action without PASS is explicitly unconfirmed", () => { const v = M.verification({ inspected_at: "2026-10-30T09:00:00Z", google_verdict: "NEUTRAL" }, { requested_at: "2026-10-29T08:00:00Z" }, NOW); assert.equal(v.key, "checked"); });
test("empty data is supported", () => { const d = M.build(null, null, null, NOW); assert.equal(d.items.length, 0); });
test("filter search and source group compose", () => { const { items } = build(fixture()); assert.equal(M.select(items, { group: "recovery", q: "missing" }).length, 1); assert.equal(M.select(items, { site: "two.example" }).length, 0); });
test("CSV neutralizes formula payloads and quotes untrusted text", () => { const csv = M.csv([{ title: '=HYPERLINK("evil")', url: "https://one.example/", reason: "+1+1", next: "Line\nTwo" }]); assert.match(csv, /'=HYPERLINK/); assert.match(csv, /'\+1\+1/); assert.match(csv, /""evil""/); });
