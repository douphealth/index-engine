// Index Engine dashboard. No framework, no build step. All text goes through textContent (crawled titles are untrusted).
"use strict";

const LABEL = {
  INDEXED: "Indexed by Google", CRAWLED_NOT_INDEXED: "Crawled, not indexed", DISCOVERED_NOT_INDEXED: "Discovered, not crawled",
  UNKNOWN_TO_GOOGLE: "Unknown to Google", GOOGLE_STALE: "Fixed since Google's last look", GOOGLE_PICKED_OTHER: "Google picked another URL",
  NOT_INSPECTED: "No Google data yet", NOT_CHECKED: "Not fetched yet", FETCH_FAILED: "Fetch failed", BROKEN: "Broken (4xx)",
  SERVER_ERROR: "Server error (5xx)", REDIRECTS: "Redirects", NOINDEX: "noindex", ROBOTS_BLOCKED: "Blocked by robots.txt",
  CANONICAL_ELSEWHERE: "Canonical elsewhere", NOT_HTML: "Not HTML",
};
const ORDER = ["INDEXED", "GOOGLE_STALE", "GOOGLE_PICKED_OTHER", "CRAWLED_NOT_INDEXED", "DISCOVERED_NOT_INDEXED", "UNKNOWN_TO_GOOGLE",
  "NOT_INSPECTED", "NOT_CHECKED", "NOINDEX", "REDIRECTS", "CANONICAL_ELSEWHERE", "ROBOTS_BLOCKED", "BROKEN", "SERVER_ERROR", "FETCH_FAILED", "NOT_HTML"];
const BLOCKERS = new Set(["FETCH_FAILED", "BROKEN", "SERVER_ERROR", "REDIRECTS", "NOINDEX", "ROBOTS_BLOCKED", "CANONICAL_ELSEWHERE", "NOT_HTML"]);
const KNOWN = ["INDEXED", "CRAWLED_NOT_INDEXED", "DISCOVERED_NOT_INDEXED", "UNKNOWN_TO_GOOGLE", "GOOGLE_STALE", "GOOGLE_PICKED_OTHER"];
const REQUEUE_DAYS = 14;
const PAGE_SIZE = 50;
const DRIVER = {
  site_rate: "The site's overall standing with Google", inl_indexed: "Links from pages Google already indexes",
  inl_body: "Links from article text", inl_all: "Internal links in any placement", log_words: "Longer main content",
  near_dup: "Being a near-duplicate", thin: "Thin content", trust: "Unverified first-hand claims",
  log_bing_impr: "Search demand seen on Bing", bing_click: "Earning Bing clicks", log_age: "An older publish date",
  depth: "A deeper URL", in_sitemap: "Being in a sitemap", is_post: "Being a blog post (vs a page)",
};
const ACTION_SHORT = { add_links: "Add links", request: "Request indexing", differentiate: "De-duplicate", expand: "Add substance", fix_trust: "Source claims" };

const S = { summary: null, st: null, sites: {}, view: {} };

/* ------------------------------------------------------------------ helpers */
function h(tag, attrs = {}, ...kids) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === null || v === undefined || v === false) continue;
    if (k === "class") el.className = v;
    else if (k === "text") el.textContent = v;
    else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else if (k === "style") Object.assign(el.style, v);
    else el.setAttribute(k, v === true ? "" : v);
  }
  for (const c of kids.flat()) if (c !== null && c !== undefined && c !== false) el.append(c instanceof Node ? c : document.createTextNode(String(c)));
  return el;
}
function svg(tag, attrs = {}, ...kids) {
  const el = document.createElementNS("http://www.w3.org/2000/svg", tag);
  for (const [k, v] of Object.entries(attrs)) el.setAttribute(k, v);
  for (const c of kids.flat()) if (c) el.append(c instanceof Node ? c : document.createTextNode(String(c)));
  return el;
}
const fmt = (n) => (n === null || n === undefined ? "–" : Number(n).toLocaleString());
const pct = (x) => (x === null || x === undefined ? "–" : `${Math.round(x * 100)}%`);
function rel(iso) {
  if (!iso) return "never";
  const s = (Date.now() - new Date(iso).getTime()) / 1000;
  if (s < 90) return "just now";
  if (s < 3600) return `${Math.round(s / 60)} min ago`;
  if (s < 86400 * 2) return `${Math.round(s / 3600)} h ago`;
  return `${Math.round(s / 86400)} days ago`;
}
const daysSince = (iso) => (iso ? (Date.now() - new Date(iso).getTime()) / 86400000 : Infinity);
function toast(msg) {
  const t = h("div", { class: "toast", role: "status", text: msg });
  document.body.append(t);
  setTimeout(() => t.remove(), 2600);
}
function oddsSpan(p) {
  if (p === null || p === undefined || p === "") return h("span", { class: "muted", text: "–" });
  const v = Number(p);
  return h("span", { class: `odds ${v < 0.15 ? "lo" : v < 0.4 ? "mid" : "hi"}`, title: "Estimated chance Google indexes it", text: pct(v) });
}
function markIn(text, phrase) {
  const i = (text || "").toLowerCase().indexOf((phrase || "").toLowerCase());
  if (i < 0 || !phrase) return [text || ""];
  return [text.slice(0, i), h("mark", { text: text.slice(i, i + phrase.length) }), text.slice(i + phrase.length)];
}
function gscLink(prop, url) {
  return prop ? `https://search.google.com/search-console/inspect?resource_id=${encodeURIComponent(prop)}&id=${encodeURIComponent(url)}` : null;
}

const api = {
  async req(path, opts = {}) {
    const r = await fetch(path, { credentials: "same-origin", ...opts });
    if (r.status === 401) { location.replace("/login"); throw new Error("signed out"); }
    if (r.status === 404 && path.startsWith("/api/data/")) return null;
    const ct = r.headers.get("Content-Type") || "";
    const body = ct.includes("json") ? await r.json() : await r.text();
    if (!r.ok) throw new Error((body && body.error) || `HTTP ${r.status}`);
    return body;
  },
  get(path) { return this.req(path); },
  post(path, data) {
    return this.req(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(data) });
  },
};

async function loadAll() {
  const [summary, st] = await Promise.all([api.get("/api/data/summary"), api.get("/api/state")]);
  S.summary = summary;
  S.st = st;
  S.urlState = new Map(st.url_state.map((u) => [u.url, u]));
  renderChrome();
}
async function loadSite(site, force) {
  if (!S.sites[site] || force) S.sites[site] = await api.get(`/api/data/site/${encodeURIComponent(site)}`);
  return S.sites[site];
}
const setting = (k, dflt) => (S.st.settings[k] ? S.st.settings[k].value : dflt);

/* ------------------------------------------------------------------ derived numbers */
function siteNumbers(s) {
  const c = s.counts || {};
  const known = KNOWN.reduce((a, k) => a + (c[k] || 0), 0);
  return {
    urls: s.urls, indexed: c.INDEXED || 0, crawled: c.CRAWLED_NOT_INDEXED || 0, discovered: c.DISCOVERED_NOT_INDEXED || 0,
    unknown: c.UNKNOWN_TO_GOOGLE || 0, stale: (c.GOOGLE_STALE || 0) + (c.GOOGLE_PICKED_OTHER || 0),
    nodata: (c.NOT_INSPECTED || 0) + (c.NOT_CHECKED || 0), known, share: known >= 10 ? (c.INDEXED || 0) / known : null,
    conflicts: s.conflicts || 0, excluded: s.excluded || 0, earners: s.bing_earners || 0,
  };
}
function openRequests() {
  return (S.summary ? S.summary.requests : []).filter((r) => {
    const u = S.urlState.get(r.url);
    return !(u && (u.ignored || daysSince(u.requested_at) < REQUEUE_DAYS));
  });
}
function engineStatus() {
  const seen = setting("engine_last_seen", null);
  const age = seen ? daysSince(seen.at) * 24 * 60 : Infinity;
  const active = S.st.commands.find((c) => c.status === "running");
  if (active) return { cls: "ok", text: "running now" };
  if (age < 45) return { cls: "ok", text: `PC online · ${rel(seen.at)}` };
  if (age < 26 * 60) return { cls: "warn", text: `PC seen ${rel(seen.at)}` };
  return { cls: "bad", text: seen ? `PC offline · ${rel(seen.at)}` : "engine not connected" };
}

function bucketBar(counts, onPick, active) {
  const bar = h("div", { class: "bar", role: "img", "aria-label": "URL states" });
  const legend = h("div", { class: "legend" });
  const keys = ORDER.filter((k) => counts[k]);
  for (const k of keys) {
    const seg = h("span", { class: `b-${BLOCKERS.has(k) ? "blocked" : k}`, title: `${LABEL[k]}: ${counts[k]}` });
    seg.style.flex = String(counts[k]);
    bar.append(seg);
    if (onPick) {
      legend.append(h("button", { type: "button", class: active === k ? "on" : null, onclick: () => onPick(active === k ? "" : k) },
        h("span", { class: `sw b-${BLOCKERS.has(k) ? "blocked" : k}` }), `${LABEL[k]} ${fmt(counts[k])}`));
    }
  }
  return onPick ? h("div", {}, bar, legend) : bar;
}

/* ------------------------------------------------------------------ chrome */
function renderChrome() {
  const pill = document.getElementById("engine-pill");
  const es = engineStatus();
  pill.replaceChildren(h("span", { class: `dot ${es.cls}` }), es.text);
  const sel = document.getElementById("run-site");
  sel.replaceChildren(h("option", { value: "", text: "All enabled sites" }),
    ...(S.summary ? S.summary.sites : []).map((s) => h("option", { value: s.site, text: s.site })));
  const urgent = S.st.alerts.filter((a) => !a.acked && (a.severity === "high" || a.severity === "warn")).length;
  document.getElementById("b-alerts").textContent = urgent ? String(urgent) : "";
  const waiting = S.st.links.filter((l) => l.status === "proposed").length;
  const bl = document.getElementById("b-links");
  bl.textContent = waiting ? String(waiting) : "";
  bl.className = "badge soft";
}
function setTab(route) {
  for (const a of document.querySelectorAll("#tabs a")) a.classList.toggle("on", a.dataset.r === route);
}

/* ------------------------------------------------------------------ views */
function noData() {
  return h("div", { class: "card empty" }, h("h2", { text: "No data yet" }),
    h("p", { text: "The engine on your PC uploads results after its next run. Use Run now, or wait for the daily run." }));
}

function viewOverview() {
  if (!S.summary) return noData();
  const sites = S.summary.sites.map((s) => ({ s, n: siteNumbers(s) }));
  const t = sites.reduce((a, { n }) => {
    for (const k of Object.keys(n)) if (typeof n[k] === "number") a[k] = (a[k] || 0) + n[k];
    return a;
  }, {});
  const lastRun = S.st.runs[0];
  const queued = S.st.commands.find((c) => c.status === "pending" || c.status === "running");
  const reqs = openRequests();
  const openFixes = S.st.fixes.filter((f) => f.status === "open").length;

  const kpi = (v, l, cls) => h("div", { class: "card kpi" }, h("div", { class: `v ${cls || ""}`, text: v }), h("div", { class: "l", text: l }));
  const head = h("div", { class: "row" }, h("h1", { text: "Portfolio" }), h("span", { class: "spacer" }),
    h("span", { class: "pill" }, h("span", { class: `dot ${lastRun && lastRun.status === "failed" ? "bad" : "ok"}` }),
      lastRun ? `Last run ${rel(lastRun.finished_at || lastRun.started_at)} · ${lastRun.status}` : "No runs yet"),
    queued ? h("span", { class: "pill" }, h("span", { class: "dot warn" }), `Run ${queued.status} (queued ${rel(queued.created_at)})`) : null,
    h("span", { class: "pill muted", text: `Data from ${rel(S.summary.generated_at)}` }));

  const kpis = h("div", { class: "grid kpis" },
    kpi(fmt(t.urls), "URLs found on your sites"),
    kpi(fmt(t.indexed), `Indexed by Google${t.known ? ` (${pct(t.indexed / t.known)} of checked)` : ""}`, "good"),
    kpi(fmt(t.crawled), "Crawled, not indexed", "bad"),
    kpi(fmt(t.discovered + t.unknown), "Discovered or unknown"),
    kpi(fmt(t.conflicts), "In a sitemap but blocked", t.conflicts ? "warn" : ""),
    kpi(fmt(t.excluded), "Kept out on purpose"),
    kpi(fmt(t.earners), "Pages earning Bing clicks"),
  );

  const waitingLinks = S.st.links.filter((l) => l.status === "proposed").length;
  const openAlerts = S.st.alerts.filter((a) => !a.acked);
  const next = h("div", { class: "card stack" }, h("h2", { text: "Today" }),
    h("div", {}, h("a", { href: "#/requests", text: `${reqs.length} pages to request in Search Console` }),
      h("div", { class: "muted small", text: "Manual, about 10 a day per property. Ranked by expected gain." })),
    h("div", {}, h("a", { href: "#/links", text: `${waitingLinks} internal links waiting for your approval` }),
      h("div", { class: "muted small", text: "Natural anchors already in your text; your PC applies approved ones." })),
    h("div", {}, h("a", { href: "#/fixes", text: `${openFixes} open fixes` })),
    h("div", { class: "small muted", text: `Engine: ${engineStatus().text}` }));

  const imp = S.summary.impact || {};
  const rate = (a, b) => (a ? `${b} of ${a} (${pct(b / a)})` : "not measured yet");
  const impact = h("div", { class: "card stack" }, h("h2", { text: `Last ${imp.days || 30} days` }),
    h("div", { class: "row" },
      h("div", { class: "kpi" }, h("div", { class: "v good", text: `+${fmt(imp.gained || 0)}` }), h("div", { class: "l", text: "entered Google's index" })),
      h("span", { class: "spacer" }),
      h("div", { class: "kpi" }, h("div", { class: "v bad", text: `−${fmt(imp.lost || 0)}` }), h("div", { class: "l", text: "dropped out" }))),
    h("div", { class: "small" }, h("b", { text: "Requests: " }), rate((imp.requests || {}).measured, (imp.requests || {}).indexed), " indexed within 7+ days"),
    h("div", { class: "small" }, h("b", { text: "New links: " }), rate((imp.links || {}).measured, (imp.links || {}).indexed), " targets indexed"),
    h("div", { class: "small" }, h("b", { text: "Bing: " }), rate((imp.bing || {}).measured, (imp.bing || {}).crawled_or_shown), " submitted URLs crawled or shown"));

  const alertCard = h("div", { class: "card stack" }, h("h2", {}, "Alerts ", openAlerts.length ? h("span", { class: "badge", text: String(openAlerts.length) }) : null),
    openAlerts.length ? h("ul", { class: "plain" }, ...openAlerts.slice(0, 4).map((a) => h("li", { class: "alert-row small" },
      h("span", { class: `sev ${a.severity}` }), h("span", {}, h("b", { text: `${a.site || ""} ` }), a.message))))
      : h("p", { class: "muted small", text: "Nothing needs you. The engine watches every page for regressions on each run." }),
    h("a", { href: "#/alerts", class: "small", text: "All alerts →" }));

  const acts = (S.summary.actions || []).slice(0, 5);
  const actCard = h("div", { class: "card stack" }, h("h2", { text: "Best next actions" }),
    acts.length ? h("ul", { class: "plain" }, ...acts.map((a) => h("li", { class: "small" },
      h("b", { text: ACTION_SHORT[a.action] || a.action }), " · ", h("a", { href: a.url, target: "_blank", rel: "noopener", text: a.title || a.url }),
      h("div", { class: "muted tiny" }, `${a.site} · odds `, oddsSpan(a.p_now), h("span", { class: "arrow", text: "→" }), oddsSpan(a.p_after)))))
      : h("p", { class: "muted small", text: "Appears after a run with Google data." }),
    h("a", { href: "#/plan", class: "small", text: "Full plan →" }));

  const table = h("table", {},
    h("thead", {}, h("tr", {}, ...["Site", "URLs", "States", "Indexed", "Share", "Crawled", "Unknown", "Conflicts", "Bing earners"].map((x, i) =>
      h("th", { class: i && i !== 2 ? "n" : null, text: x })))),
    h("tbody", {}, ...sites.map(({ s, n }) => h("tr", { class: "click", onclick: () => (location.hash = `#/site/${s.site}`) },
      h("td", {}, h("a", { href: `#/site/${s.site}`, text: s.site }), s.disabled ? h("span", { class: "chip", text: " paused" }) : null),
      h("td", { class: "n", text: fmt(n.urls) }),
      h("td", {}, bucketBar(s.counts || {})),
      h("td", { class: "n good", text: fmt(n.indexed) }),
      h("td", { class: "n", text: pct(n.share) }),
      h("td", { class: "n", text: fmt(n.crawled) }),
      h("td", { class: "n", text: fmt(n.unknown) }),
      h("td", { class: `n ${n.conflicts ? "warn" : ""}`, text: fmt(n.conflicts) }),
      h("td", { class: "n", text: fmt(n.earners) }),
    ))));

  return h("div", {}, head, kpis, h("div", { class: "cols" },
    h("div", { class: "card" }, h("h2", { text: "Google indexed share of checked URLs" }), trendChart()), next),
    h("div", { class: "grid kpis", style: { gridTemplateColumns: "repeat(auto-fit, minmax(280px, 1fr))" } }, actCard, impact, alertCard),
    h("h3", { text: "Sites" }), h("div", { class: "tablewrap" }, table));
}

function trendChart() {
  const pts = S.st.runs.filter((r) => r.status === "ok" && r.summary && r.summary.totals && r.summary.totals.known)
    .map((r) => ({ t: new Date(r.finished_at || r.started_at).getTime(), y: r.summary.totals.indexed / r.summary.totals.known }))
    .reverse();
  if (pts.length < 2) return h("p", { class: "muted small", text: "The trend appears after the second run with Google data." });
  const W = 600, H = 160, P = 28;
  const t0 = pts[0].t, t1 = pts[pts.length - 1].t || t0 + 1;
  const ymax = Math.max(0.1, ...pts.map((p) => p.y)) * 1.15;
  const x = (t) => P + ((t - t0) / Math.max(1, t1 - t0)) * (W - 2 * P);
  const y = (v) => H - P + 6 - (v / ymax) * (H - 2 * P);
  const line = pts.map((p) => `${x(p.t).toFixed(1)},${y(p.y).toFixed(1)}`).join(" ");
  return svg("svg", { class: "chart", viewBox: `0 0 ${W} ${H}`, role: "img", "aria-label": "Indexed share over time" },
    ...[0, 0.5, 1].map((f) => svg("line", { class: "grid-line", x1: P, x2: W - P, y1: y(ymax * f / 1.15), y2: y(ymax * f / 1.15) })),
    ...[0, 0.5, 1].map((f) => svg("text", { x: 2, y: y(ymax * f / 1.15) + 4 }, pct(ymax * f / 1.15))),
    svg("polyline", { class: "l1", points: line }),
    svg("text", { x: P, y: H - 4 }, new Date(t0).toLocaleDateString()),
    svg("text", { x: W - P - 60, y: H - 4 }, new Date(t1).toLocaleDateString()));
}

async function viewSite(site) {
  const meta = S.summary && S.summary.sites.find((s) => s.site === site);
  if (!meta) return h("div", { class: "card empty", text: `No data for ${site} yet.` });
  const data = await loadSite(site);
  if (!data) return h("div", { class: "card empty", text: `No URL data for ${site} yet.` });
  const v = (S.view[site] ||= { bucket: "", flag: "", q: "", ignored: false, page: 0, open: null });
  const col = Object.fromEntries(data.cols.map((c, i) => [c, i]));
  const n = siteNumbers(meta);
  const wrap = h("div", {});

  const flags = [...new Set(data.rows.flatMap((r) => (r[col.flags] || "").split(",").filter(Boolean)))].sort();
  const filtered = () => data.rows.filter((r) => {
    const st = S.urlState.get(r[col.url]);
    if (!v.ignored && st && st.ignored) return false;
    if (v.bucket && r[col.bucket] !== v.bucket) return false;
    if (v.flag && !(r[col.flags] || "").split(",").includes(v.flag)) return false;
    if (v.q) {
      const q = v.q.toLowerCase();
      if (!(r[col.url].toLowerCase().includes(q) || (r[col.title] || "").toLowerCase().includes(q))) return false;
    }
    return true;
  });

  const tableBox = h("div", {});
  function drawTable() {
    const rows = filtered();
    const pages = Math.max(1, Math.ceil(rows.length / PAGE_SIZE));
    v.page = Math.min(v.page, pages - 1);
    const slice = rows.slice(v.page * PAGE_SIZE, (v.page + 1) * PAGE_SIZE);
    const body = h("tbody");
    for (const r of slice) {
      const url = r[col.url], st = S.urlState.get(url) || {};
      const tr = h("tr", { class: `click ${st.ignored ? "dim" : ""}`, onclick: () => { v.open = v.open === url ? null : url; drawTable(); } },
        h("td", { class: "page" }, h("div", { text: r[col.title] || url }), h("div", { class: "u", text: url })),
        h("td", {}, h("span", { class: "pill" }, h("span", { class: `sw b-${BLOCKERS.has(r[col.bucket]) ? "blocked" : r[col.bucket]}` }), LABEL[r[col.bucket]] || r[col.bucket]),
          st.ignored ? h("span", { class: "chip", text: "ignored" }) : null),
        h("td", { class: "n" }, r[col.bucket] === "INDEXED" || BLOCKERS.has(r[col.bucket]) ? h("span", { class: "muted", text: "–" }) : oddsSpan(r[col.odds])),
        h("td", { class: "hide-sm small", text: r[col.bing] || "" }),
        h("td", { class: "n hide-sm", text: (+r[col.value]).toFixed(1) }));
      body.append(tr);
      if (v.open === url) body.append(detailRow(site, meta, r, col, st, drawTable));
    }
    tableBox.replaceChildren(
      h("div", { class: "tablewrap" }, h("table", {},
        h("thead", {}, h("tr", {}, h("th", { text: `Page (${fmt(rows.length)})` }), h("th", { text: "State" }),
          h("th", { class: "n", text: "Odds", title: "Estimated chance Google indexes it (model trained on your sites)" }),
          h("th", { class: "hide-sm", text: "Bing" }), h("th", { class: "n hide-sm", text: "Value" }))),
        slice.length ? body : h("tbody", {}, h("tr", {}, h("td", { colspan: 5, class: "empty", text: "No URLs match." }))))),
      h("div", { class: "pager" }, h("span", { class: "muted small", text: `Page ${v.page + 1} of ${pages}` }),
        h("button", { class: "btn small", disabled: v.page === 0, onclick: () => { v.page--; drawTable(); } }, "Previous"),
        h("button", { class: "btn small", disabled: v.page >= pages - 1, onclick: () => { v.page++; drawTable(); } }, "Next")));
  }

  const pick = (b) => { v.bucket = b; v.page = 0; render(); };
  const controls = h("div", { class: "row" },
    h("input", { type: "search", placeholder: "Search URL or title", value: v.q, "aria-label": "Search",
      oninput: (e) => { v.q = e.target.value; v.page = 0; drawTable(); } }),
    h("select", { "aria-label": "Filter by state", onchange: (e) => pick(e.target.value) },
      h("option", { value: "", text: "All states" }),
      ...ORDER.filter((k) => meta.counts[k]).map((k) => h("option", { value: k, selected: v.bucket === k, text: `${LABEL[k]} (${meta.counts[k]})` }))),
    h("select", { "aria-label": "Filter by flag", onchange: (e) => { v.flag = e.target.value; v.page = 0; drawTable(); } },
      h("option", { value: "", text: "All flags" }), ...flags.map((f) => h("option", { value: f, selected: v.flag === f, text: f }))),
    h("label", { class: "chk small" }, h("input", { type: "checkbox", checked: v.ignored, onchange: (e) => { v.ignored = e.target.checked; drawTable(); } }), "Show ignored"));

  drawTable();
  wrap.append(
    h("div", { class: "row" }, h("a", { href: "#/", text: "← Portfolio" })),
    h("div", { class: "row" }, h("h1", { text: site }), h("span", { class: "spacer" }),
      h("a", { class: "btn small", href: `https://${site}/`, target: "_blank", rel: "noopener" }, "Open site"),
      h("button", { class: "btn small", onclick: () => openRunDialog(site) }, "Run this site")),
    h("div", { class: "grid kpis" },
      ...[[fmt(n.urls), "URLs"], [fmt(n.indexed), `Indexed (${pct(n.share)} of checked)`], [fmt(n.crawled), "Crawled, not indexed"],
        [fmt(n.discovered + n.unknown), "Discovered or unknown"], [fmt(n.conflicts), "In a sitemap but blocked"], [fmt(n.earners), "Bing earners"]]
        .map(([a, b]) => h("div", { class: "card kpi" }, h("div", { class: "v", text: a }), h("div", { class: "l", text: b })))),
    h("div", { class: "card" }, h("h2", { text: "Diagnosis" }),
      h("ul", { class: "diag" }, ...(meta.diagnosis.length ? meta.diagnosis : [["ok", "Nothing to flag."]]).map(([k, m]) => h("li", { class: k, text: m })))),
    h("h3", { text: "URLs" }), bucketBar(meta.counts, pick, v.bucket), h("div", { class: "row" }, controls), h("div", { style: { height: "8px" } }), tableBox,
    linkPlan(data));
  return wrap;
}

function detailRow(site, meta, r, col, st, redraw) {
  const url = r[col.url];
  const act = async (action, extra = {}) => {
    try {
      const res = await api.post("/api/url", { url, site, action, ...extra });
      S.urlState.set(url, res.url_state);
      toast(action === "note" ? "Note saved" : `Marked ${action}`);
      redraw();
    } catch (e) { toast(e.message); }
  };
  const note = h("textarea", { "aria-label": "Note", placeholder: "Note for yourself (kept in the dashboard)" });
  note.value = st.note || "";
  const link = gscLink(meta.gsc_property, url);
  let why = [], trust = [];
  try { why = JSON.parse(r[col.why] || "[]"); } catch { /* older data */ }
  try { trust = JSON.parse(r[col.trust] || "[]"); } catch { /* older data */ }
  return h("tr", { class: "detail" }, h("td", { colspan: 5 }, h("div", { class: "stack" },
    h("div", {}, h("b", { text: "Why: " }), r[col.reason] || "–"),
    why.length && r[col.bucket] !== "INDEXED" ? h("div", {}, h("b", { text: "What holds it back (model): " }), why.join("; ")) : null,
    r[col.action] ? h("div", {}, h("b", { text: "What to do: " }), r[col.action]) : null,
    trust.length ? h("div", { class: "small" }, h("b", { text: "Claims to verify:" }),
      h("ul", {}, ...trust.map((t) => h("li", {}, h("span", { class: "chip", text: t.kind }), `“${t.excerpt}”`)))) : null,
    h("div", {}, ...(r[col.flags] || "").split(",").filter(Boolean).map((f) => h("span", { class: "chip", text: f })),
      h("span", { class: "chip", text: `${r[col.kind]} · ${fmt(r[col.words])} words · ${+r[col.in_sitemap] ? "in sitemap" : "not in sitemap"}` }),
      r[col.google] ? h("span", { class: "chip", text: `Google: ${r[col.google]}` }) : null),
    h("div", { class: "row" },
      h("a", { class: "btn small", href: url, target: "_blank", rel: "noopener" }, "Open page"),
      link ? h("a", { class: "btn small", href: link, target: "_blank", rel: "noopener" }, "Inspect in Search Console") : null,
      st.ignored ? h("button", { class: "btn small", onclick: () => act("unignore") }, "Stop ignoring")
        : h("button", { class: "btn small", title: "Hide from work lists and never submit", onclick: () => act("ignore") }, "Ignore"),
      st.requested_at ? h("span", { class: "chip", text: `requested ${rel(st.requested_at)}` }) : null),
    note, h("div", {}, h("button", { class: "btn small", onclick: () => act("note", { note: note.value }) }, "Save note")))));
}

function linkPlan(data) {
  if (!data.plan || !data.plan.length) return null;
  return h("div", {}, h("h3", { text: "Internal links to add (from pages Google indexes or that earn Bing clicks)" }),
    h("div", { class: "tablewrap" }, h("table", {},
      h("thead", {}, h("tr", {}, h("th", { text: "On this page" }), h("th", { text: "Link to" }), h("th", { class: "hide-sm", text: "Shared terms" }))),
      h("tbody", {}, ...data.plan.slice(0, 40).map((p) => h("tr", {},
        h("td", { class: "page" }, h("a", { href: p.source, target: "_blank", rel: "noopener", text: p.source_title || p.source })),
        h("td", { class: "page" }, h("a", { href: p.target, target: "_blank", rel: "noopener", text: p.target_title || p.target })),
        h("td", { class: "hide-sm small muted", text: p.shared_terms })))))));
}

async function markUrl(url, site, action) {
  const res = await api.post("/api/url", { url, site, action });
  S.urlState.set(url, res.url_state);
}

function viewPlan() {
  if (!S.summary) return noData();
  const m = S.summary.model || {};
  const acts = S.summary.actions || [];
  const v = (S.view.__plan ||= { site: "", action: "" });
  const props = Object.fromEntries(S.summary.sites.map((s) => [s.site, s.gsc_property]));
  const byTarget = {};
  for (const l of S.st.links) (byTarget[l.target] ||= []).push(l);
  const rows = acts.filter((a) => (!v.site || a.site === v.site) && (!v.action || a.action === v.action));
  const total = rows.reduce((t, a) => t + (a.p_after - a.p_now), 0);

  const drivers = (m.drivers || []).filter((d) => DRIVER[d.feature]).slice(0, 6);
  const modelCard = h("div", { class: "card stack" }, h("h2", { text: "What decides indexing on your sites" }),
    m.n ? h("p", { class: "small" }, `Learned from ${fmt(m.n)} pages Google has judged (${fmt(m.indexed)} indexed). `,
      m.auc_within_site ? `Ranking accuracy within a site, on pages held out of training (AUC): ${m.auc_within_site} - ${m.quality}. ` : "",
      m.auc ? `Across all sites: ${m.auc}. ` : "", m.note || "") : h("p", { class: "muted small", text: m.note || "No model yet." }),
    drivers.length ? h("ul", { class: "plain small" }, ...drivers.map((d) => h("li", {},
      h("span", { class: d.direction === "raises" ? "good" : "bad", text: d.direction === "raises" ? "▲ " : "▼ " }),
      `${DRIVER[d.feature]} → ${d.direction === "raises" ? "higher" : "lower"} odds`))) : null,
    h("p", { class: "muted tiny", text: "Correlations in your own data, not proof of cause. Odds are re-estimated after every run, and actions you take are measured on the Overview." }));

  const doIt = (a) => {
    if (a.action === "request") {
      const st = S.urlState.get(a.url) || {};
      if (daysSince(st.requested_at) < REQUEUE_DAYS) return h("span", { class: "chip", text: `requested ${rel(st.requested_at)}` });
      const link = gscLink(props[a.site], a.url);
      return h("div", { class: "row" }, link ? h("a", { class: "btn small", href: link, target: "_blank", rel: "noopener" }, "Open in Search Console") : null,
        h("button", { class: "btn small primary", onclick: async () => { try { await markUrl(a.url, a.site, "requested"); render(); } catch (e) { toast(e.message); } } }, "Done"));
    }
    if (a.action === "add_links") {
      const ls = byTarget[a.url] || [];
      const open = ls.filter((l) => l.status === "proposed").length;
      const live = ls.filter((l) => l.status === "applied").length;
      return ls.length ? h("a", { class: "btn small", href: `#/links?target=${encodeURIComponent(a.url)}` },
        open ? `Review ${open} proposed link${open > 1 ? "s" : ""}` : `${live} applied`) : h("span", { class: "muted tiny", text: "No natural anchor found yet: add a link by hand" });
    }
    return h("a", { class: "btn small", href: a.url, target: "_blank", rel: "noopener" }, "Open page");
  };

  return h("div", { class: "stack" }, h("h1", { text: "Plan" }),
    h("p", { class: "muted", text: "Every action below is ranked by expected gain: how much it should raise the page's odds of being indexed, weighted by what the page is worth to you (clicks, impressions). Do them top-down." }),
    modelCard,
    h("div", { class: "row" },
      h("select", { "aria-label": "Site", onchange: (e) => { v.site = e.target.value; render(); } },
        h("option", { value: "", text: "All sites" }), ...[...new Set(acts.map((a) => a.site))].map((s) => h("option", { value: s, selected: v.site === s, text: s }))),
      h("div", { class: "seg-ctl" }, ...[["", "All"], ...Object.entries(ACTION_SHORT)].map(([k, l]) =>
        h("button", { type: "button", class: v.action === k ? "on" : null, onclick: () => { v.action = k; render(); } }, l))),
      h("span", { class: "spacer" }), h("span", { class: "muted small", text: `${rows.length} actions · about ${total.toFixed(1)} more pages expected indexed if all are done` })),
    rows.length ? h("div", { class: "tablewrap" }, h("table", {},
      h("thead", {}, h("tr", {}, ...["Action", "Page", "Odds now → after", "Do it"].map((x) => h("th", { text: x })))),
      h("tbody", {}, ...rows.map((a) => h("tr", {},
        h("td", {}, h("b", { text: ACTION_SHORT[a.action] || a.action }), h("div", { class: "muted tiny", text: a.label })),
        h("td", { class: "page" }, h("a", { href: a.url, target: "_blank", rel: "noopener", text: a.title || a.url }),
          h("div", { class: "u", text: `${a.site} · ${LABEL[a.bucket] || a.bucket}` })),
        h("td", { class: "n" }, oddsSpan(a.p_now), h("span", { class: "arrow", text: "→" }), oddsSpan(a.p_after)),
        h("td", {}, doIt(a)))))))
      : h("div", { class: "card empty", text: "No actions for this filter." }));
}

function viewLinks(params) {
  const v = (S.view.__links ||= { tab: "proposed", site: "" });
  const target = params.get("target");
  const groups = { proposed: ["proposed"], approved: ["approved", "undo"], applied: ["applied"], other: ["rejected", "failed", "undone"] };
  const all = S.st.links.filter((l) => (!v.site || l.site === v.site) && (!target || l.target === target));
  const shown = all.filter((l) => groups[v.tab].includes(l.status));
  const act = async (ids, action) => {
    try {
      const r = await api.post("/api/link", { ids, action });
      for (const l of S.st.links) if (ids.includes(l.id) && r.changed) l.status = r.status;
      toast(`${r.changed} link${r.changed === 1 ? "" : "s"}: ${r.status}`);
      renderChrome();
      render();
    } catch (e) { toast(e.message); }
  };
  const autopilot = setting("link_autopilot", false);
  const cap = setting("links_per_site_per_day", 5);
  const card = (l) => h("div", { class: "card stack" },
    h("div", { class: "row small" }, h("span", { class: "chip", text: l.site }), h("span", { class: "chip", text: l.status }),
      l.gain ? h("span", { class: "muted tiny", text: `expected gain ${(+l.gain).toFixed(2)}` }) : null),
    h("div", {}, "On ", h("a", { href: l.source, target: "_blank", rel: "noopener", text: l.source_title || l.source }),
      h("span", { class: "arrow", text: " link to " }), h("a", { href: l.target, target: "_blank", rel: "noopener", text: l.target_title || l.target })),
    h("div", { class: "small muted" }, "“", ...markIn(l.context, l.anchor), "”"),
    l.result ? h("div", { class: `tiny ${l.status === "failed" ? "bad" : "muted"}`, text: l.result }) : null,
    h("div", { class: "row" },
      l.status === "proposed" ? [h("button", { class: "btn small primary", onclick: () => act([l.id], "approve") }, "Approve"),
        h("button", { class: "btn small", onclick: () => act([l.id], "reject") }, "Reject")] : null,
      l.status === "approved" ? h("button", { class: "btn small", onclick: () => act([l.id], "cancel") }, "Cancel") : null,
      l.status === "applied" ? h("button", { class: "btn small", onclick: () => act([l.id], "undo") }, "Undo") : null,
      ["rejected", "failed", "undone"].includes(l.status) ? h("button", { class: "btn small", onclick: () => act([l.id], "reopen") }, "Reopen") : null));
  return h("div", { class: "stack" }, h("h1", { text: "Internal links" }),
    h("div", { class: "card small stack" },
      h("div", {}, "Links from pages Google already indexes are the strongest lever for pages Google knows but won't index. Each proposal links a phrase that ",
        h("b", { text: "already exists" }), " in the source page's text; nothing else on the page changes."),
      h("div", {}, `Your PC applies approved links within 30 minutes: at most ${cap} per site per day, with a backup, a live check and Undo. `,
        `Autopilot is ${autopilot ? "ON (it also applies the best proposals itself, except on gearuptofit)" : "off (only links you approve are applied)"}. `,
        h("a", { href: "#/settings", text: "Settings" }))),
    target ? h("div", { class: "row small" }, h("span", { text: `Showing links to ${target}` }), h("a", { href: "#/links", text: "show all" })) : null,
    h("div", { class: "row" },
      h("div", { class: "seg-ctl" }, ...Object.keys(groups).map((k) => h("button", { type: "button", class: v.tab === k ? "on" : null,
        onclick: () => { v.tab = k; render(); } }, `${k[0].toUpperCase()}${k.slice(1)} (${all.filter((l) => groups[k].includes(l.status)).length})`))),
      h("select", { "aria-label": "Site", onchange: (e) => { v.site = e.target.value; render(); } },
        h("option", { value: "", text: "All sites" }), ...[...new Set(S.st.links.map((l) => l.site))].map((s) => h("option", { value: s, selected: v.site === s, text: s }))),
      h("span", { class: "spacer" }),
      v.tab === "proposed" && shown.length ? h("button", { class: "btn small", onclick: () => act(shown.slice(0, 100).map((l) => l.id), "approve") },
        `Approve all ${Math.min(100, shown.length)} shown`) : null),
    shown.length ? shown.map(card) : h("div", { class: "card empty", text: "Nothing here." }));
}

function viewAlerts() {
  const ack = async (body) => { try { await api.post("/api/alert", body); await loadAll(); render(); } catch (e) { toast(e.message); } };
  const open = S.st.alerts.filter((a) => !a.acked);
  return h("div", { class: "stack" }, h("div", { class: "row" }, h("h1", { text: "Alerts" }), h("span", { class: "spacer" }),
    open.length ? h("button", { class: "btn small", onclick: () => ack({ all: true }) }, "Acknowledge all") : null),
    h("p", { class: "muted small", text: "Raised automatically: an indexable page that turns noindex, 404s, redirects or loses its canonical; content that shrinks; a sitemap that drops URLs; a site whose pages stop answering; pages entering or leaving Google's index." }),
    S.st.alerts.length ? S.st.alerts.map((a) => h("div", { class: `card alert-row ${a.acked ? "acked" : ""}` },
      h("span", { class: `sev ${a.severity}`, title: a.severity }),
      h("div", { style: { flex: "1", minWidth: "0" } }, h("div", {}, h("b", { text: `${a.site || "portfolio"} · ` }), a.message),
        a.url ? h("a", { class: "tiny", href: a.url, target: "_blank", rel: "noopener", text: a.url }) : null,
        h("div", { class: "muted tiny", text: rel(a.at) })),
      a.acked ? null : h("button", { class: "btn small", onclick: () => ack({ id: a.id }) }, "OK")))
      : h("div", { class: "card empty", text: "No alerts yet." }));
}

function viewRequests() {
  if (!S.summary) return noData();
  const open = openRequests();
  const done = S.summary.requests.filter((r) => { const u = S.urlState.get(r.url); return u && daysSince(u.requested_at) < REQUEUE_DAYS; });
  const props = Object.fromEntries(S.summary.sites.map((s) => [s.site, s.gsc_property]));
  const mark = async (r, action) => {
    try {
      const res = await api.post("/api/url", { url: r.url, site: r.site, action });
      S.urlState.set(r.url, res.url_state);
      render();
    } catch (e) { toast(e.message); }
  };
  const bySite = {};
  for (const r of open) (bySite[r.site] ||= []).push(r);
  const card = (r, isDone) => h("div", { class: "card row" },
    h("div", { class: "page", style: { flex: "1", minWidth: "220px" } }, h("div", { text: r.title || r.url }),
      h("div", { class: "muted tiny" }, `${LABEL[r.bucket] || r.bucket} · odds `, oddsSpan(r.odds), ` · ${r.url}`)),
    isDone ? h("button", { class: "btn small", onclick: () => mark(r, "unrequested") }, "Undo")
      : [gscLink(props[r.site], r.url) ? h("a", { class: "btn small", href: gscLink(props[r.site], r.url), target: "_blank", rel: "noopener" }, "Open in Search Console") : null,
        h("button", { class: "btn small primary", onclick: () => mark(r, "requested") }, "Done")]);
  return h("div", { class: "stack" },
    h("h1", { text: "Request indexing" }),
    h("p", { class: "muted", text: "Search Console's Request indexing button has no API and a small daily limit (about 10 per property). " +
      "This list holds only pages where a request can help: Google has never seen them, they were fixed since Google's last look, or they changed after Google crawled them. " +
      "Open each one, click Request indexing in Search Console, then mark it done here. Done pages stay off the list for 14 days." }),
    open.length ? Object.entries(bySite).map(([site, rs]) => h("div", { class: "stack" }, h("h3", { text: `${site} (${rs.length})` }), ...rs.map((r) => card(r, false))))
      : h("div", { class: "card empty", text: "Nothing to request right now." }),
    done.length ? h("details", {}, h("summary", { class: "muted", text: `Done in the last 14 days (${done.length})` }),
      h("div", { class: "stack" }, ...done.map((r) => card(r, true)))) : null);
}

function viewFixes() {
  const set = async (f, status) => {
    try { await api.post("/api/fix", { id: f.id, status }); f.status = status; render(); } catch (e) { toast(e.message); }
  };
  const copy = async (text) => { try { await navigator.clipboard.writeText(text); toast("Copied"); } catch { toast("Copy failed"); } };
  return h("div", { class: "stack" }, h("h1", { text: "Fixes" }),
    h("p", { class: "muted", text: "Changes to the live sites that need you: a command to run on your PC, or a setting in wp-admin. Each script runs a dry run first, backs up before it writes, and prints its own rollback." }),
    ...S.st.fixes.map((f) => h("div", { class: "card stack" },
      h("div", { class: "row" }, h("span", { class: `pill` }, h("span", { class: `dot ${f.status === "open" ? "warn" : "ok"}` }), f.status),
        h("span", { class: "muted small", text: f.site }), h("span", { class: "spacer" }),
        f.status === "open" ? [h("button", { class: "btn small primary", onclick: () => set(f, "done") }, "Mark done"),
          h("button", { class: "btn small", onclick: () => set(f, "skipped") }, "Skip")]
          : h("button", { class: "btn small", onclick: () => set(f, "open") }, "Reopen")),
      h("h2", { text: f.title }), h("p", { class: "small", text: f.detail || "" }),
      f.how ? h("div", {}, h("pre", { class: "code", text: f.how }), h("button", { class: "btn small", onclick: () => copy(f.how) }, "Copy")) : null)));
}

function viewRuns() {
  const logBox = h("pre", { class: "log", text: "Loading the last run's log…" });
  api.get("/api/data/log").then((t) => { logBox.textContent = t || "No log uploaded yet."; }).catch((e) => { logBox.textContent = e.message; });
  const cancel = async (c) => { try { await api.post("/api/command", { cancel: c.id }); await loadAll(); render(); } catch (e) { toast(e.message); } };
  const dur = (r) => (r.finished_at ? `${Math.max(1, Math.round((new Date(r.finished_at) - new Date(r.started_at)) / 60000))} min` : "–");
  const opts = (o) => Object.entries(o || {}).map(([k, v]) => (v === true ? k : `${k}=${v}`)).join(", ") || "all sites";
  return h("div", { class: "stack" }, h("h1", { text: "Runs" }),
    h("h3", { text: "Queue" }),
    S.st.commands.length ? h("div", { class: "tablewrap" }, h("table", {},
      h("thead", {}, h("tr", {}, ...["Queued", "Options", "Status", "Result", ""].map((x) => h("th", { text: x })))),
      h("tbody", {}, ...S.st.commands.slice(0, 10).map((c) => h("tr", {},
        h("td", { class: "small", text: rel(c.created_at) }), h("td", { class: "small", text: opts(c.options) }),
        h("td", { text: c.status }), h("td", { class: "small muted", text: (c.result || "").slice(0, 140) }),
        h("td", {}, c.status === "pending" ? h("button", { class: "btn small", onclick: () => cancel(c) }, "Cancel") : null))))))
      : h("p", { class: "muted", text: "Nothing queued." }),
    h("h3", { text: "History" }),
    S.st.runs.length ? h("div", { class: "tablewrap" }, h("table", {},
      h("thead", {}, h("tr", {}, ...["Started", "Took", "Status", "Trigger", "Options", "Indexed / checked", "Submitted"].map((x, i) => h("th", { class: i > 4 ? "n" : null, text: x })))),
      h("tbody", {}, ...S.st.runs.slice(0, 40).map((r) => {
        const t = (r.summary && r.summary.totals) || {};
        return h("tr", {}, h("td", { class: "small", text: new Date(r.started_at).toLocaleString() }), h("td", { text: dur(r) }),
          h("td", { class: r.status === "failed" ? "bad" : r.status === "ok" ? "good" : "", text: r.status, title: r.error || "" }),
          h("td", { text: r.trigger || "" }), h("td", { class: "small", text: opts(r.options) }),
          h("td", { class: "n", text: t.known ? `${fmt(t.indexed)} / ${fmt(t.known)}` : "–" }), h("td", { class: "n", text: fmt(t.submitted) }));
      }))))
      : h("p", { class: "muted", text: "No runs reported yet." }),
    h("h3", { text: "Last run log" }), logBox);
}

function viewSettings() {
  const sites = S.summary ? S.summary.sites.map((s) => s.site) : [];
  const disabled = new Set(setting("sites_disabled", []));
  const checks = sites.map((s) => h("label", { class: "chk" }, h("input", { type: "checkbox", value: s, checked: !disabled.has(s) }), s));
  const paused = h("input", { type: "checkbox", checked: setting("submit_paused", false) });
  const queue = h("input", { type: "number", min: 0, max: 50, value: setting("manual_queue_per_site", 10) });
  const inspect = h("input", { type: "number", min: 0, max: 2000, placeholder: "no cap", value: setting("inspect_max_per_site", null) ?? "" });
  const time = h("input", { type: "time", value: setting("daily_run_time", "10:30") });
  const freshChk = h("input", { type: "checkbox", checked: setting("fresh_check", true) });
  const autopilot = h("input", { type: "checkbox", checked: setting("link_autopilot", false) });
  const linkCap = h("input", { type: "number", min: 0, max: 10, value: setting("links_per_site_per_day", 5) });
  const save = async () => {
    const body = {
      sites_disabled: checks.map((c) => c.firstChild).filter((i) => !i.checked).map((i) => i.value),
      submit_paused: paused.checked,
      manual_queue_per_site: Number(queue.value || 0),
      inspect_max_per_site: inspect.value === "" ? null : Number(inspect.value),
      daily_run_time: time.value || "10:30",
      fresh_check: freshChk.checked,
      link_autopilot: autopilot.checked,
      links_per_site_per_day: Math.max(0, Math.min(10, Number(linkCap.value || 0))),
    };
    try { await api.post("/api/settings", body); await loadAll(); toast("Saved. The engine applies it on its next run."); } catch (e) { toast(e.message); }
  };
  const field = (label, input, help) => h("div", { class: "stack" }, h("label", { class: "stack small" }, label, input), help ? h("div", { class: "muted tiny", text: help }) : null);
  return h("div", { class: "stack" }, h("h1", { text: "Settings" }),
    h("div", { class: "card stack" }, h("h2", { text: "Sites" }), h("div", { class: "grid kpis" }, ...checks),
      h("div", { class: "muted tiny", text: "Unticked sites are skipped by scheduled and queued runs." })),
    h("div", { class: "card stack" }, h("h2", { text: "Engine" }),
      h("label", { class: "chk" }, paused, "Pause all submissions (Bing, IndexNow, Search Console sitemaps)"),
      field("Daily run time on your PC", time, "The engine checks every 30 minutes and runs once a day after this time. Google's inspection quota resets at midnight Pacific (10:00 in Greece)."),
      field("Request-indexing list size per site", queue, "Search Console allows roughly 10 manual requests a day per property."),
      field("Max URL inspections per site per run", inspect, "Leave empty to use the full daily quota (about 1,950)."),
      h("label", { class: "chk" }, freshChk, "Fresh-content autopilot: every 30 minutes, find new or updated posts and get them audited, inspected and submitted within minutes"),
      h("div", {}, h("button", { class: "btn primary", onclick: save }, "Save settings"))),
    h("div", { class: "card stack" }, h("h2", { text: "Internal links" }),
      field("Links applied per site per day (max 10)", linkCap, "Approved links beyond this wait for the next day. 0 pauses link changes."),
      h("label", { class: "chk" }, autopilot, "Autopilot: also apply the best-ranked proposals without waiting for approval (never on gearuptofit, whose rules require your click)"),
      h("div", { class: "muted tiny", text: "Every applied link is backed up, verified on the live page, logged, and can be undone from the Links page." }),
      h("div", {}, h("button", { class: "btn primary", onclick: save }, "Save settings"))),
    h("div", { class: "card stack" }, h("h2", { text: "About" }),
      h("p", { class: "small muted", text: "The engine runs on your PC with your Google and Bing credentials; they are never uploaded here. " +
        "This dashboard stores results, your decisions (ignored URLs, requests done, fixes) and the run queue. " +
        "Google's Indexing API is never used for articles: Google limits it to job-posting and livestream pages." })));
}

/* ------------------------------------------------------------------ router */
async function render() {
  const app = document.getElementById("app");
  const [path, query] = location.hash.replace(/^#\/?/, "").split("?");
  const [route, arg] = path.split("/");
  const params = new URLSearchParams(query || "");
  setTab(route === "site" ? "" : route || "");
  try {
    let node;
    if (route === "site" && arg) node = await viewSite(decodeURIComponent(arg));
    else if (route === "plan") node = viewPlan();
    else if (route === "links") node = viewLinks(params);
    else if (route === "alerts") node = viewAlerts();
    else if (route === "requests") node = viewRequests();
    else if (route === "fixes") node = viewFixes();
    else if (route === "runs") node = viewRuns();
    else if (route === "settings") node = viewSettings();
    else node = viewOverview();
    app.replaceChildren(node);
  } catch (e) {
    app.replaceChildren(h("div", { class: "card empty", text: `Could not load: ${e.message}` }));
  }
}

function openRunDialog(site = "") {
  const dlg = document.getElementById("run-dlg");
  document.getElementById("run-site").value = site;
  dlg.showModal();
}

document.getElementById("run-btn").addEventListener("click", () => openRunDialog());
// The form's submit event fires synchronously with the button press (the dialog's close event is queued and can be
// delayed in background tabs). method="dialog" then closes the dialog by itself.
document.getElementById("run-form").addEventListener("submit", async (e) => {
  if (!e.submitter || e.submitter.value !== "ok") return;
  const options = {
    site: document.getElementById("run-site").value || undefined,
    no_submit: document.getElementById("run-nosubmit").checked,
    force_audit: document.getElementById("run-force").checked,
    dry_run: document.getElementById("run-dry").checked,
  };
  try {
    const r = await api.post("/api/command", { kind: "run", options });
    toast(r.note || "Run queued. Your PC picks it up within 30 minutes.");
    await loadAll();
    render();
  } catch (err) { toast(err.message); }
});
document.getElementById("logout").addEventListener("click", async () => {
  await api.post("/api/auth/logout", {}).catch(() => {});
  location.replace("/login");
});
window.addEventListener("hashchange", render);
setInterval(async () => { if (!document.hidden) { await loadAll().catch(() => {}); } }, 120000);

loadAll().then(render).catch((e) => {
  document.getElementById("app").replaceChildren(h("div", { class: "card empty", text: `Could not load: ${e.message}` }));
});
