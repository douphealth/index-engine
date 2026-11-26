"use strict";
// The frame around every page: the menu must reach every page the app can draw, and "Search or jump" must find
// pages, websites and actions from a few typed letters.
const test = require("node:test"), assert = require("node:assert/strict");
const fs = require("node:fs"), vm = require("node:vm"), path = require("node:path");
const pub = (f) => fs.readFileSync(path.join(__dirname, "../public", f), "utf8");
vm.runInThisContext(pub("shell.js") + "\n;globalThis.ShellModel = ShellModel;");
const M = globalThis.ShellModel;
const html = pub("index.html"), app = pub("app.js");
const SITES = ["micegoneguide.com", "gearuptofit.com", "gearuptogrow.com", "tenderatlas.shop"];
const labels = (text, sites = SITES) => M.match(M.entries(sites), text).map((e) => e.label);

test("the menu links to every page the app can draw, and to nothing else", () => {
  const routes = JSON.parse(app.match(/const route = (\[[^\]]+\])\.includes\(asked\)/)[1]).filter((r) => r !== "site");
  const linked = [...html.matchAll(/data-r="([a-z]*)"/g)].map((m) => m[1]);
  assert.deepEqual([...linked].sort(), ["", ...routes].sort());
  assert.equal(new Set(linked).size, linked.length, "a page is in the menu twice");
  for (const r of linked) assert.ok(html.includes(`href="#/${r}" data-r="${r}"`), `menu link for "${r}" points elsewhere`);
});

test("search or jump knows the same pages as the menu", () => {
  const menu = [...html.matchAll(/<a href="([^"]+)"(?: data-r="[a-z]*")?><svg/g)].map((m) => m[1]).sort();
  assert.deepEqual(M.PAGES.map((p) => p[1]).sort(), menu);
});

test("the ids the app's code looks up are all in the page", () => {
  for (const id of ["tabs", "b-alerts", "b-links", "engine-pill", "run-btn", "logout", "app", "run-dlg", "run-form", "run-site", "run-nosubmit",
    "run-force", "run-dry", "run-cancel", "page-title", "side", "side-shade", "menu-btn", "theme-btn", "jump-btn", "jump-btn-top", "jump-dlg",
    "jump-input", "jump-list"]) {
    assert.equal(html.split(`id="${id}"`).length - 1, 1, `id="${id}" must be in index.html exactly once`);
  }
});

test("the page keeps to the content security policy: no inline script, style or handler", () => {
  assert.equal(/<script(?![^>]*\bsrc=)[^>]*>/.test(html), false, "inline <script>");
  assert.equal(/<style[\s>]/.test(html), false, "inline <style>");
  assert.equal(/\sstyle=/.test(html), false, "style attribute");
  assert.equal(/\son[a-z]+=/.test(html), false, "inline event handler");
  assert.ok(html.indexOf('src="/theme.js"') < html.indexOf('src="/app.js"'), "the theme is applied before the app draws");
});

test("an empty box lists the pages and the general actions, never one row per website", () => {
  const got = M.match(M.entries(SITES), "", 50);
  assert.equal(got.filter((e) => e.kind === "page").length, M.PAGES.length);
  assert.deepEqual(got.filter((e) => e.kind !== "page").map((e) => e.label),
    ["Run the engine for all websites", "Switch theme (light, dark, auto)", "Sign out"]);
});

test("a few letters of a website and a page find that website's page", () => {
  assert.equal(labels("mice gr")[0], "micegoneguide.com: growth");
  assert.equal(labels("tender links")[0], "tenderatlas.shop: links");
  const e = M.match(M.entries(SITES), "gearuptofit recovery")[0];
  assert.deepEqual([e.kind, e.href], ["site", "#/recovery?site=gearuptofit.com"]);
  assert.equal(M.match(M.entries(SITES), "gearuptofit overview")[0].href, "#/site/gearuptofit.com");
});

test("every typed word must match, in any order, and keywords count", () => {
  assert.deepEqual(labels("growth mice"), ["micegoneguide.com: growth"]);
  assert.deepEqual(labels("zzz"), []);
  assert.equal(labels("dark")[0], "Switch theme (light, dark, auto)");            // found by a keyword
  assert.equal(labels("request indexing")[0], "Requests");
  assert.ok(labels("run").includes("Run the engine for all websites"));
  assert.equal(labels("run mice")[0], "Run the engine for micegoneguide.com");
});

test("a label that starts with the text ranks above one that only contains it", () => {
  const got = labels("gear", SITES);
  assert.ok(got.every((l) => l.includes("gear")));
  assert.ok(got[0].startsWith("gearuptofit.com"), "the first website in the list comes first among equals");
  assert.equal(labels("re")[0], "Recovery");                                       // pages before websites' pages
  assert.equal(labels("links")[0], "Links");
});

test("typed text is matched literally, whatever characters it has", () => {
  for (const text of ["(", "[a-z", ".*", "\\", "a+b?", "$^"]) assert.doesNotThrow(() => M.match(M.entries(SITES), text));
  assert.deepEqual(labels(".*"), []);
});

test("a website's address is encoded into the link", () => {
  const e = M.entries(["a b.example/x"]).find((x) => x.kind === "site" && x.label.endsWith(": growth"));
  assert.equal(e.href, "#/growth?site=a%20b.example%2Fx");
});

test("the result list is capped", () => {
  assert.equal(M.match(M.entries(SITES), "e", 12).length, 12);
  assert.equal(M.match(M.entries(SITES), "e", 5).length, 5);
});

test("the theme button goes light, dark, auto, light", () => {
  assert.deepEqual(["auto", "light", "dark"].map(M.nextTheme), ["light", "dark", "auto"]);
  assert.equal(M.nextTheme(undefined), "light");
});

test("the growth page names and counts the grouped small searches", () => {
  assert.ok(/long_tail: \["Many small searches, one topic"/.test(app));
  assert.ok(app.includes("(o.close_to_top || 0) + (o.low_ctr || 0) + (o.long_tail || 0)"));
});
