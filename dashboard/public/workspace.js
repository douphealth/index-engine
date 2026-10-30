/* Live operational workspace. All displayed page text is untrusted and inserted as text. */
"use strict";
const W = globalThis.IndexWorkspace;
const $ = id => document.getElementById(id);
const state = { summary: null, owner: {}, health: null, tables: new Map(), items: [], coverage: [], errors: [], page: 0, selected: null, loading: false, saving: false, dirty: false, epoch: 0 };
const PAGE = 40;
function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, val] of Object.entries(attrs)) {
    if (val === null || val === undefined || val === false) continue;
    if (key === "text") node.textContent = String(val);
    else if (key.startsWith("on")) node.addEventListener(key.slice(2), val);
    else node.setAttribute(key, val === true ? "" : String(val));
  }
  for (const child of children.flat()) if (child !== null && child !== undefined) node.append(child instanceof Node ? child : document.createTextNode(String(child)));
  return node;
}
function notice(message, bad = false) { $("notice").textContent = message; $("notice").className = bad ? "error" : ""; }
const format = n => Number(n).toLocaleString();
const date = v => Number.isFinite(Date.parse(v)) ? new Date(v).toLocaleString() : "Not supplied";
const val = v => v === "" || v === null || v === undefined ? "Not supplied" : String(v);
function link(href, text) { return el("a", { href, text, class: "button", ...(href.startsWith("http") ? { target: "_blank", rel: "noopener noreferrer" } : {}) }); }
function button(text, click, primary = false, disabled = false) { return el("button", { type: "button", class: "button" + (primary ? " primary" : ""), onclick: click, disabled, text }); }
async function request(path, data) {
  const ctl = new AbortController(), timer = setTimeout(() => ctl.abort(), 25000);
  try {
    const r = await fetch(path, { credentials: "same-origin", signal: ctl.signal,
      ...(data ? { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(data) } : {}) });
    if (r.status === 401) { location.assign("/login"); throw new Error("Sign in again to continue."); }
    if (r.status === 404 && path.startsWith("/api/data/")) return null;
    const isJSON = (r.headers.get("Content-Type") || "").includes("json");
    const payload = isJSON ? await r.json() : null;
    if (!r.ok) throw new Error(payload?.error || "Request failed (HTTP " + r.status + ").");
    if (!isJSON || !payload || typeof payload !== "object") throw new Error("The server did not return valid dashboard data.");
    return payload;
  } catch (error) {
    if (error.name === "AbortError") throw new Error(data ? "Request timed out. Its outcome is unknown; refresh the queue before retrying." : "Data request timed out. Refresh to retry.");
    throw error;
  } finally { clearTimeout(timer); }
}
function options() { return { site: $("site").value, group: $("group").value, mode: $("mode").value, q: $("search").value, includePaused: $("paused").checked, includeIgnored: $("ignored").checked }; }
function filtered() { return W.select(state.items, options()); }
function rebuild() {
  const model = W.build(state.summary, state.owner, state.tables);
  state.items = model.items; state.coverage = model.coverage;
}
function updateCoverage() {
  const loaded = state.coverage.filter(s => s.loaded);
  const total = state.summary?.sites?.length || 0;
  const bad = state.coverage.filter(s => !s.loaded).map(s => s.site);
  $("coverage").textContent = (state.loading ? "Loading: " : "Evidence coverage: ") + loaded.length + " / " + total + " sites loaded. "
    + "Snapshot: " + date(state.summary?.generated_at) + (bad.length && !state.loading ? ". Unavailable: " + bad.join(", ") : "")
    + (state.errors.length ? ". " + state.errors.join(" ") : "")
    + (state.coverage.some(s => s.invalid) ? ". Invalid URL records were skipped; inspect the source snapshot." : "");
  const active = W.select(state.items, { ...options(), mode: "all", group: "", q: "" });
  const waiting = active.filter(r => ["awaiting", "stale"].includes(r.followup.key)).length;
  $("metrics").replaceChildren(...[[active.length, "Loaded URLs in scope"], [active.filter(r => r.review).length, "Need review, not automatic edits"], [waiting, "Awaiting current follow-up"], [active.filter(r => r.bucket === "INDEXED").length, "Reported indexed, not live count"]]
    .map(([n, text]) => el("div", { class: "metric" }, el("b", { text: format(n) }), el("span", { text }))));
  const seen = W.setting(state.owner, "engine_last_seen", null);
  const days = W.age(seen?.at);
  $("heartbeat").textContent = days !== null && days * 1440 < 25 ? "Engine checked in: " + date(seen.at) : "No recent engine heartbeat. Queued work depends on the local engine.";
}
function render() {
  updateCoverage();
  const rows = filtered();
  const pages = Math.max(1, Math.ceil(rows.length / PAGE));
  state.page = Math.max(0, Math.min(state.page, pages - 1));
  if (!rows.some(r => r.url === state.selected)) state.selected = rows[0]?.url || null;
  $("count").textContent = format(rows.length) + " URLs";
  $("page").textContent = (state.page + 1) + " / " + pages;
  $("previous").disabled = state.page === 0;
  $("next").disabled = state.page === pages - 1;
  $("export").disabled = !rows.length;
  $("rows").replaceChildren(...(rows.length ? rows.slice(state.page * PAGE, (state.page + 1) * PAGE).map(row => {
    const select = () => {
      if (state.dirty && !confirm("Discard the unsaved note before opening another URL?")) return;
      state.dirty = false; state.selected = row.url; render();
      if (matchMedia("(max-width:760px)").matches) $("detail").scrollIntoView({ block: "start", behavior: "auto" });
    };
    return el("tr", { class: state.selected === row.url ? "selected" : "" },
      el("td", {}, el("button", { class: "page-link", type: "button", onclick: select, text: row.title, "aria-pressed": state.selected === row.url ? "true" : "false" }), el("span", { class: "domain", text: row.site })),
      el("td", {}, el("span", { text: row.label }), el("br"), el("span", { class: "tag", text: row.group }), row.paused ? el("span", { class: "tag warn", text: "Paused" }) : null, row.ignored ? el("span", { class: "tag warn", text: "Ignored" }) : null),
      el("td", {}, el("span", { class: "tag" + (row.stale ? " warn" : ""), text: row.stale ? "Refresh evidence" : "Recent audit" }), el("span", { class: "domain", text: row.clicksKnown ? format(row.clicks) + " recorded clicks" : "Click data unavailable" })));
  }) : [el("tr", {}, el("td", { colspan: "3", class: "empty", text: state.loading ? "Loading website evidence..." : state.coverage.some(s => !s.loaded) ? "No matching loaded URLs. Some website evidence is unavailable; refresh or review the engine." : "No URLs match these filters. This does not prove there are no indexation issues." }))]));
  if (!state.dirty) showDetail(rows.find(r => r.url === state.selected));
}
async function mutate(path, data, message) {
  if (state.saving) return;
  state.saving = true;
  const controls = Array.from(document.querySelectorAll("#detail button,#doctor"), b => [b, b.disabled]);
  controls.forEach(([b]) => b.disabled = true);
  try {
    const result = await request(path, data);
    if (result.ok === false) throw new Error("No change was confirmed. Refresh the workspace before retrying.");
    if (result.url_state) {
      const entries = state.owner.url_state || [];
      state.owner.url_state = entries.filter(x => x.url !== result.url_state.url).concat(result.url_state);
      state.dirty = false; rebuild();
    }
    if (path === "/api/command") {
      try { state.owner = await request("/api/state"); } catch { /* accepted command already reported; do not misreport failure */ }
    }
    notice(result.note || message);
  } catch (error) { notice(error.message, true); }
  finally { state.saving = false; controls.forEach(([b, wasDisabled]) => b.disabled = wasDisabled); rebuild(); render(); }
}
function showDetail(row) {
  if (!row) { $("detail").replaceChildren(el("div", { class: "empty", text: "Select a URL to see its evidence and next action." })); return; }
  const siteRoute = "/#/site/" + encodeURIComponent(row.site);
  const gsc = row.meta.gsc_property ? "https://search.google.com/search-console/inspect?resource_id=" + encodeURIComponent(row.meta.gsc_property) + "&id=" + encodeURIComponent(row.url) : null;
  const audit = () => {
    if (state.dirty) { notice("Save or discard your note before queueing an audit.", true); return; }
    if (!confirm("Queue a measure-only audit for " + row.site + "? This re-fetches its pages without submitting URLs or editing WordPress. Google inspection follows the engine's quota and schedule.")) return;
    mutate("/api/command", { kind: "run", options: { site: row.site, no_submit: true, force_audit: true } }, "Measure-only audit queued. The local engine must be running; check Runs for completion.");
  };
  const saveAction = action => mutate("/api/url", { site: row.site, url: row.url, action }, action === "requested" ? "Recorded as manually requested, not verified indexed. A later inspection is required." : "Saved dashboard decision.");
  const note = el("textarea", { id: "url-note", maxlength: "2000", "aria-label": "Note for this URL" });
  note.value = row.owner.note || "";
  note.addEventListener("input", () => { state.dirty = true; });
  const facts = [
    ["Crawl audit", date(row.checked_at)], ["HTTP response", val(row.http_status)], ["Canonical", val(row.canonical)],
    ["Google robots", row.robots_google === 1 ? "Allowed in audit" : row.robots_google === 0 ? "Blocked in audit" : "Not supplied"],
    ["Successful inspection", date(row.inspected_at)], ["Google verdict", val(row.google_verdict)],
    ["Coverage", val(row.google_coverage)], ["Google canonical", val(row.google_canonical)], ["Google last crawl", date(row.last_crawl)],
    ["Latest API error", row.google_error ? "Last inspection attempt failed; previous evidence retained" : "No error supplied"],
    ["Requested manually", date(row.owner.requested_at)], ["Link applied", date(row.linked_at)],
    ["Google clicks / pull", row.gsc_clicks === "" || row.gsc_clicks === undefined ? "Not supplied" : val(row.gsc_clicks) + " / " + date(row.gsc_at)],
    ["Bing clicks / pull", row.bing_clicks === "" || row.bing_clicks === undefined ? "Not supplied" : val(row.bing_clicks) + " / " + date(row.bing_at)],
  ];
  const markRequested = () => {
    if (!confirm("Only continue after using Request indexing in Google Search Console for this URL. Record that manual action now? This does not send a Google request.")) return;
    saveAction("requested");
  };
  $("detail").replaceChildren(el("div", {},
    el("span", { class: "tag", text: row.label }), el("h2", { text: row.title }), el("span", { class: "url", text: row.url }),
    row.stale ? el("div", { class: "callout" }, el("b", { text: "Evidence needs refreshing" }), el("p", { text: "An upload timestamp is not a crawl timestamp. Older engine versions do not supply per-URL dates. Re-audit after updating the engine before treating this as current." })) : null,
    el("div", { class: "callout" }, el("b", { text: "Next action" }), el("p", { text: row.next })),
    el("p", { class: "small", text: row.reason || "No diagnosis supplied by the engine." }),
    el("div", { class: "actions" }, link(row.url, "Open page"), link(siteRoute, "Full URL analysis"), gsc ? link(gsc, "Inspect in Search Console") : link("/#/connections", "Resolve GSC access")),
    el("h3", { text: "Recorded evidence" }), el("dl", { class: "evidence" }, ...facts.flatMap(([k, v]) => [el("dt", { text: k }), el("dd", { text: v })])),
    el("div", { class: "callout" }, el("b", { text: "Follow-up" }), el("p", { text: row.followup.label }), el("p", { text: "A later indexing observation does not establish that the action caused it. Requesting and submitting are not verification." })),
    el("div", { class: "actions" }, button("Re-audit website", audit, true, row.paused), link("/#/links?target=" + encodeURIComponent(row.url), "Review links"), link("/#/revival?site=" + encodeURIComponent(row.site), "Traffic recovery")),
    el("h3", { text: "Decisions & notes" }),
    el("div", { class: "actions" }, button(row.ignored ? "Stop ignoring" : "Ignore this URL", () => saveAction(row.ignored ? "unignore" : "ignore")), gsc ? button("Record manual request", markRequested, false, row.paused) : null,
      row.owner.requested_at ? button("Undo request record", () => saveAction("unrequested")) : null),
    el("label", { for: "url-note", class: "small", text: "Saved across devices through the dashboard" }), note,
    button("Save note", () => mutate("/api/url", { site: row.site, url: row.url, action: "note", note: note.value }, "Note saved.")),
    row.paused ? el("p", { class: "small", text: "This site is paused. Review it without queueing changes; manage its status in Settings." }) : null));
}
async function refresh() {
  if (state.loading || state.saving) return;
  if (state.dirty && !confirm("Discard your unsaved note and refresh the evidence?")) return;
  state.dirty = false; state.loading = true; $("refresh").disabled = true;
  const epoch = ++state.epoch;
  try {
    notice("Refreshing recorded evidence...");
    const [summary, owner, health] = await Promise.all([request("/api/data/summary"), request("/api/state"), request("/api/data/health").catch(() => null)]);
    if (!summary || !Array.isArray(summary.sites)) throw new Error("No portfolio snapshot is available. Update and run the local engine, then refresh.");
    if (!Array.isArray(owner.url_state) || !owner.settings) throw new Error("Dashboard state is incomplete. No actions were changed.");
    state.summary = summary; state.owner = owner; state.health = health; state.tables.clear(); state.errors = [];
    const currentSite = $("site").value;
    $("site").replaceChildren(el("option", { value: "", text: "All websites" }), ...summary.sites.map(s => el("option", { value: s.site, text: s.site })));
    $("site").value = summary.sites.some(s => s.site === currentSite) ? currentSite : "";
    const pending = summary.sites.slice();
    rebuild(); render();
    // At most three requests at once. Failures remain visible instead of counting as healthy sites.
    const worker = async () => {
      while (pending.length && epoch === state.epoch) {
        const site = pending.shift().site;
        try { const table = await request("/api/data/site/" + encodeURIComponent(site)); if (table) state.tables.set(site, table); }
        catch (e) { state.errors.push(site + ": " + e.message); }
        rebuild(); render();
      }
    };
    await Promise.all(Array.from({ length: Math.min(3, pending.length) }, worker));
    notice(state.coverage.some(s => !s.loaded) ? "Partial evidence only. Unavailable websites are listed above; do not interpret missing data as zero issues." : "Evidence refreshed. All counts describe loaded snapshots, not live search-engine state.", state.coverage.some(s => !s.loaded));
  } catch (error) { notice(error.message, true); }
  finally { state.loading = false; $("refresh").disabled = false; render(); }
}
function changeFilters() {
  if (state.dirty && !confirm("Discard the unsaved note and change filters?")) return;
  state.dirty = false; state.page = 0;
  const p = new URLSearchParams(); const o = options();
  for (const k of ["site", "group", "mode", "q"]) if (o[k]) p.set(k, o[k]);
  try { history.replaceState(null, "", location.pathname + "?" + p); } catch { /* restricted embedded contexts still support filtering */ }
  render();
}
const initial = new URLSearchParams(location.search);
for (const key of ["group", "mode"]) if (initial.has(key)) $(key).value = initial.get(key);
$("search").value = initial.get("q") || "";
for (const key of ["site", "group", "mode", "paused", "ignored"]) $(key).addEventListener("change", changeFilters);
let searchTimer;
$("search").addEventListener("input", () => { clearTimeout(searchTimer); searchTimer = setTimeout(changeFilters, 160); });
$("previous").addEventListener("click", () => { state.page--; render(); });
$("next").addEventListener("click", () => { state.page++; render(); });
$("refresh").addEventListener("click", refresh);
$("doctor").addEventListener("click", () => mutate("/api/command", { kind: "doctor" }, "Read-only diagnostic queued. Check Connections for the uploaded result."));
$("export").addEventListener("click", () => {
  const url = URL.createObjectURL(new Blob(["\ufeff", W.csv(filtered())], { type: "text/csv;charset=utf-8" }));
  const a = el("a", { href: url, download: "indexation-evidence.csv" }); document.body.append(a); a.click(); a.remove(); setTimeout(() => URL.revokeObjectURL(url), 1000);
});
try { const theme = localStorage.getItem("ie-workspace-theme"); if (["dark", "light"].includes(theme)) document.documentElement.dataset.theme = theme; } catch { /* UI preference only */ }
$("theme").addEventListener("click", () => {
  const dark = document.documentElement.dataset.theme ? document.documentElement.dataset.theme === "dark" : matchMedia("(prefers-color-scheme:dark)").matches;
  const theme = dark ? "light" : "dark"; document.documentElement.dataset.theme = theme;
  try { localStorage.setItem("ie-workspace-theme", theme); } catch { /* storage can be blocked */ }
});
window.addEventListener("beforeunload", e => { if (state.dirty) { e.preventDefault(); e.returnValue = ""; } });
setInterval(() => { if (!document.hidden && !state.dirty && !state.loading && !state.saving) refresh(); }, 120000);
refresh().then(() => { if (initial.get("site")) { $("site").value = initial.get("site"); render(); } });
