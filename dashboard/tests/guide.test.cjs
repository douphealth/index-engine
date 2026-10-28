"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
// The browser script assigns IndexGuide on globalThis. This package uses
// `"type": "module"`, so requiring the .js file yields no CommonJS exports.
require("../public/guide.js");
const { derive } = globalThis.IndexGuide;

const NOW = Date.parse("2026-10-27T12:00:00Z");
const sample = () => ({
  summary: {
    generated_at: "2026-10-27T11:50:00Z",
    sites: [
      { site: "one.example", urls: 100,
        counts: { INDEXED: 35, CRAWLED_NOT_INDEXED: 24, DISCOVERED_NOT_INDEXED: 10, UNKNOWN_TO_GOOGLE: 4, NOINDEX: 7, BROKEN: 2 },
        conflicts: 4, gsc_property: "sc-domain:one.example", bing_site: "https://one.example" },
      { site: "two.example", urls: 12, counts: { INDEXED: 2, BROKEN: 3 }, conflicts: 1, gsc_property: null },
    ],
    requests: [{ url: "https://one.example/a", site: "one.example" },
      { url: "https://one.example/b", site: "one.example" },
      { url: "https://two.example/c", site: "two.example" }],
    actions: [{ site: "one.example", action: "add_links" }, { site: "two.example", action: "request" }],
    impact: { gained: 3, lost: 1 },
  },
  state: {
    settings: { engine_last_seen: { value: { at: "2026-10-27T11:55:00Z" } },
      submit_paused: { value: false }, sites_disabled: { value: [] } },
    runs: [{ status: "ok", finished_at: "2026-10-27T11:50:00Z" }],
    links: [{ site: "one.example", status: "proposed" }, { site: "two.example", status: "approved" }],
    alerts: [{ site: "one.example", severity: "high", acked: 0 }],
    commands: [],
    url_state: [{ url: "https://one.example/a", ignored: 1 },
      { url: "https://one.example/b", requested_at: "2026-10-26T12:00:00Z" }],
  },
});

test("shows real counts without treating uninspected URLs as confirmed non-indexed", () => {
  const { summary, state } = sample();
  const d = derive(summary, state, { now: NOW });
  assert.equal(d.totalSites, 2);
  assert.equal(d.urls, 112);
  assert.equal(d.indexed, 37);
  assert.equal(d.problems, 12); // 7 noindex + 2 broken + 3 broken
  assert.equal(d.toImprove, 38);
  assert.equal(d.requests, 1); // ignored and recently marked are excluded
  assert.equal(d.proposed, 1);
  assert.equal(d.steps.length, 5);
  assert.equal(d.steps[1].href, "#/site/one.example");
});

test("disable a site removes its work and URLs but preserves total portfolio count", () => {
  const { summary, state } = sample();
  state.settings.sites_disabled.value = ["one.example"];
  const d = derive(summary, state, { now: NOW });
  assert.equal(d.totalSites, 2);
  assert.equal(d.activeSites, 1);
  assert.equal(d.urls, 12);
  assert.equal(d.proposed, 0);
  assert.equal(d.requests, 1);
  assert.equal(d.highAlerts, 0);
  assert.equal(d.sites[0].enabled, false);
});

test("focus filter narrows work to one enabled website", () => {
  const { summary, state } = sample();
  const d = derive(summary, state, { now: NOW, focus: "one.example" });
  assert.equal(d.totalSites, 1);
  assert.equal(d.urls, 100);
  assert.equal(d.requests, 0);
  assert.equal(d.proposed, 1);
  assert.equal(d.actions, 1);
});

test("reports offline and stale data honestly", () => {
  const { summary, state } = sample();
  state.settings.engine_last_seen.value.at = "2026-10-25T00:00:00Z";
  summary.generated_at = "2026-10-24T11:00:00Z";
  const d = derive(summary, state, { now: NOW });
  assert.equal(d.connected, false);
  assert.equal(d.fresh, false);
  assert.equal(d.steps[0].status, "attention");
  assert.match(d.steps[0].detail, /PC/);
});

test("keeps indexing submissions separate from observed outcomes", () => {
  const { summary, state } = sample();
  const d = derive(summary, state, { now: NOW });
  assert.equal(d.impact.gained, 3);
  assert.match(d.steps[4].detail, /not proof/);
  assert.equal(d.steps[4].href, "#/overview");
});

test("does not crash while a first run is pending or data are empty", () => {
  const d = derive(null, null, { now: NOW });
  assert.equal(d.urls, 0);
  assert.equal(d.activeSites, 0);
  assert.equal(d.steps[0].status, "attention");
  assert.equal(d.steps[4].status, "attention");
});

test("assesses connected status only from recent heartbeats", () => {
  const { summary, state } = sample();
  assert.equal(derive(summary, state, { now: NOW }).connected, true);
  state.settings.engine_last_seen.value.at = "2026-10-27T11:34:00Z";
  assert.equal(derive(summary, state, { now: NOW }).connected, false);
});

test("Google indexed share is withheld for a small sample", () => {
  const { summary, state } = sample();
  const d = derive(summary, state, { now: NOW });
  assert.equal(d.sites[0].share, 35 / 73);
  assert.equal(d.sites[1].share, null);
});

const withTraffic = (verdicts) => {
  const { summary, state } = sample();
  summary.sites[0].traffic = {
    bing: { clicks: { verdict: verdicts.bing, pct: -33, confidence: "high" }, clicks_prev: 1037, clicks_now: 695, end: "2026-10-25" },
    google: { clicks: { verdict: verdicts.google, pct: 5, confidence: null }, clicks_prev: 40, clicks_now: 42, end: "2026-10-25" },
  };
  return { summary, state };
};

test("a clear traffic drop becomes the step that points at Revival", () => {
  const { summary, state } = withTraffic({ bing: "declining", google: "stable" });
  const d = derive(summary, state, { now: NOW });
  assert.equal(d.drops.length, 1);
  assert.deepEqual([d.drops[0].site, d.drops[0].src, d.drops[0].prev, d.drops[0].now], ["one.example", "bing", 1037, 695]);
  const step = d.steps[2];
  assert.equal(step.title, "Recover lost search traffic");
  assert.equal(step.status, "attention");
  assert.equal(step.href, "#/revival?site=one.example");
  assert.match(step.detail, /Bing clicks fell 33% \(1,037 to 695\)/);
});

test("traffic that is only noisy or small is never called a drop", () => {
  const { summary, state } = withTraffic({ bing: "stable", google: "too_little_data" });
  const d = derive(summary, state, { now: NOW });
  assert.equal(d.drops.length, 0);
  assert.equal(d.steps[2].status, "clear");
  assert.equal(d.steps[2].metric, "No clear traffic drop");
});

test("without any traffic history the step says so instead of reporting good news", () => {
  const { summary, state } = sample();
  const d = derive(summary, state, { now: NOW });
  assert.equal(d.hasTraffic, false);
  assert.equal(d.steps[2].metric, "No traffic history yet");
  assert.equal(d.steps[2].status, "ready");
});

test("a paused site's traffic drop is not raised", () => {
  const { summary, state } = withTraffic({ bing: "declining", google: "stable" });
  state.settings.sites_disabled = { value: ["one.example"] };
  assert.equal(derive(summary, state, { now: NOW }).drops.length, 0);
});
