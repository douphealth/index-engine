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
    else if (k === "href" && !/^(https?:|#|\/)/i.test(String(v))) continue;      // never javascript:, data: ... from crawled data
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
function toast(msg, ms = 2600) {
  for (const old of document.querySelectorAll(".toast")) old.remove();         // one at a time, never stacked
  const t = h("div", { class: "toast", role: "status", text: msg });
  document.body.append(t);
  setTimeout(() => t.remove(), ms);
}
// Crawled text reaches us with HTML entities (&#8220;). An inert parser turns them into characters; nothing is executed.
function decodeEntities(text) {
  return new DOMParser().parseFromString(`<!doctype html><body>${String(text || "")}`, "text/html").body.textContent;
}
function oddsSpan(p) {
  if (p === null || p === undefined || p === "") return h("span", { class: "muted", text: "–" });
  const v = Number(p);
  return h("span", { class: `odds ${v < 0.15 ? "lo" : v < 0.4 ? "mid" : "hi"}`, title: "Estimated chance Google indexes it",
    text: v > 0 && v < 0.005 ? "<1%" : pct(v) });
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
// Paused means paused in Settings now; the published results only know what was true when they were made.
const isPaused = (site) => (setting("sites_disabled", []) || []).includes(site);
function preset(v, params, keys) {
  if (!params || v.presetFor === location.hash) return;
  v.presetFor = location.hash;
  for (const k of keys) if (params.has(k)) { v[k] = params.get(k); v.page = 0; }
}

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
  if (age < 25) return { cls: "ok", text: `PC online · ${rel(seen.at)}` };
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
    ...(S.summary ? S.summary.sites : []).map((s) => h("option", { value: s.site, disabled: isPaused(s.site), text: s.site + (isPaused(s.site) ? " (paused in Settings)" : "") })));
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

/* ------------------------------------------------------------------ priorities
   One ranked to-do list for the whole portfolio: urgent problems first, then quick wins ranked by expected pages
   indexed per minute of your time, then technical fixes, then content work. Expected gains come from the odds model
   (simulated "after" odds minus current odds, summed over pages); they are estimates, labelled with "≈". */
const TIER = ["Urgent", "Quick win", "Technical", "Content"];
const MIN_PER_PAGE = { add_links: 3, fix_trust: 10, differentiate: 30, expand: 45 };
const effortText = (m) => (m < 60 ? `${Math.max(1, Math.round(m))} min` : `about ${Math.round(m / 60)} h`);
const plural = (n, word) => `${fmt(n)} ${word}${n === 1 ? "" : "s"}`;
function requestRate() {
  const r = (S.summary.impact || {}).requests || {};
  return r.measured >= 20 ? Math.max(0.03, r.indexed / r.measured) : 0.1;
}
function priorities() {
  const out = [];
  const add = (o) => out.push({ gain: null, minutes: 5, ...o });
  const live = S.summary.sites.filter((s) => !isPaused(s.site));
  const liveSet = new Set(live.map((s) => s.site));
  const es = engineStatus();
  const seen = setting("engine_last_seen", null);
  const bySite = (list) => list.reduce((m, x) => ((m[x.site || "portfolio"] ||= []).push(x), m), {});
  const downOf = (s) => ((s.counts || {}).SERVER_ERROR || 0) + ((s.counts || {}).FETCH_FAILED || 0);
  const downHref = (s) => `#/site/${s.site}?bucket=${(s.counts || {}).SERVER_ERROR ? "SERVER_ERROR" : "FETCH_FAILED"}`;

  // ---- urgent
  if (es.cls === "bad") {
    add({ tier: 0, title: "Turn on your PC: the engine is offline", minutes: 1, cta: "Runs", href: "#/runs",
      why: `${seen ? `Last seen ${rel(seen.at)}` : "Never connected"}. Until it runs, no link is applied, no page is submitted or checked.` });
  }
  if (setting("submit_paused", false)) {
    add({ tier: 0, title: "Everything is paused", minutes: 1, cta: "Resume", href: "#/settings",
      why: "Submissions and link edits are switched off in Settings, so the engine only measures. Nothing you approve is applied until you resume." });
  }
  if (es.cls !== "bad" && daysSince(S.summary.generated_at) > 1.5) {
    add({ tier: 0, title: "The results are out of date", minutes: 5, cta: "See runs", href: "#/runs",
      why: `The last full run finished ${rel(S.summary.generated_at)} although your PC is online. Open the run log to see what stopped it.` });
  }
  const lastRun = S.st.runs[0];
  if (lastRun && lastRun.status === "failed") {
    add({ tier: 0, title: "The last run failed", why: (lastRun.error || "See the log on the Runs page.").slice(0, 160), cta: "See log", href: "#/runs" });
  }
  const openAlerts = S.st.alerts.filter((a) => !a.acked);
  for (const [site, as] of Object.entries(bySite(openAlerts.filter((a) => a.severity === "high")))) {
    add({ tier: 0, site, title: `${plural(as.length, "urgent alert")} on ${site}`, why: as[0].message, cta: "Open alerts", href: "#/alerts" });
  }
  for (const s of live) {
    const down = downOf(s);
    if (down && down / Math.max(1, s.urls) >= 0.05) {
      add({ tier: 0, site: s.site, title: `${plural(down, "page")} on ${s.site} do not answer`, minutes: 15, cta: "See pages", href: downHref(s),
        why: "Server errors and timeouts make Google slow down crawling of the whole site. Fix the server first." });
    }
  }

  // ---- quick wins: ranked by expected pages per minute
  const proposed = S.st.links.filter((l) => l.status === "proposed" && liveSet.has(l.site));
  for (const [site, ls] of Object.entries(bySite(proposed))) {
    const pot = ((S.summary.sites.find((s) => s.site === site) || {}).potential || {}).add_links || {};
    const perPage = pot.n ? pot.pages / pot.n : 0.05;               // simulated effect of +3 links on one page
    const perTarget = {};
    for (const l of ls) perTarget[l.target] = (perTarget[l.target] || 0) + 1;
    const gain = Object.values(perTarget).reduce((g, k) => g + perPage * Math.min(1, k / 3), 0);
    add({ tier: 1, site, gain, minutes: Math.max(1, ls.length * 0.5), cta: "Review links", href: `#/links?site=${encodeURIComponent(site)}`,
      title: `Approve ${plural(ls.length, "internal link")} on ${site}`,
      why: `They point at ${plural(Object.keys(perTarget).length, "page")} Google has not indexed, from pages it already indexes. ` +
        "The anchor text is already on your page. Your PC adds approved links within 10 minutes." });
  }
  const rate = requestRate();
  const perDay = setting("manual_queue_per_site", 10) || 10;
  for (const [site, rs] of Object.entries(bySite(openRequests().filter((r) => liveSet.has(r.site))))) {
    const today = rs.slice(0, perDay);
    const gain = today.reduce((g, r) => g + rate * (1 - (r.odds === "" || r.odds === null || r.odds === undefined ? 0.2 : +r.odds)), 0);
    add({ tier: 1, site, gain, minutes: today.length, cta: "Request", href: `#/requests?site=${encodeURIComponent(site)}`,
      title: `Request indexing for ${plural(today.length, "page")} on ${site}`,
      why: `Pages Google has not crawled, or that changed since it did. Search Console takes only about ${perDay} a day per site` +
        `${rs.length > today.length ? ` (${rs.length - today.length} more wait for the next days)` : ""}.` });
  }

  // ---- technical
  for (const [site, as] of Object.entries(bySite(openAlerts.filter((a) => a.severity === "warn")))) {
    add({ tier: 2, site, title: `${plural(as.length, "warning")} on ${site}`, why: as[0].message, cta: "Open alerts", href: "#/alerts" });
  }
  for (const s of live) {
    if (!s.gsc_property) {
      add({ tier: 2, site: s.site, title: `Add ${s.site} to Google Search Console`, minutes: 10, cta: "Open Search Console",
        href: "https://search.google.com/search-console/welcome",
        why: "Without it the engine cannot see what Google thinks of these pages or submit the sitemap. Verify the domain, then add the engine's " +
          "service account (client_email in its Google credential file) under Settings → Users and permissions, with Full access." });
    }
    if (!s.bing_site) {
      add({ tier: 2, site: s.site, title: `Add ${s.site} to Bing Webmaster Tools`, minutes: 5, cta: "Open Bing", href: "https://www.bing.com/webmasters",
        why: "Import it from Search Console (fastest). IndexNow submissions already work without it, but Bing clicks and crawl data need it." });
    }
  }
  for (const f of S.st.fixes.filter((x) => x.status === "open")) {
    add({ tier: 2, site: f.site, title: f.title, why: (f.detail || "").slice(0, 180), cta: "Open fix", href: "#/fixes" });
  }
  for (const s of live) {
    const fl = s.flags || {};
    const down = downOf(s);
    if (down && down / Math.max(1, s.urls) < 0.05) {
      add({ tier: 2, site: s.site, title: `${plural(down, "page")} on ${s.site} failed to load`, minutes: 10, cta: "See pages", href: downHref(s),
        why: "Check them: a page that errors for crawlers cannot be indexed." });
    }
    if (s.conflicts) {
      add({ tier: 2, site: s.site, title: `${plural(s.conflicts, "URL")} in the ${s.site} sitemaps ${s.conflicts === 1 ? "is" : "are"} blocked`, minutes: 10,
        why: "Each one is listed in a sitemap but noindexed, redirected, broken or canonical elsewhere. Mixed signals waste crawl budget.",
        cta: "See diagnosis", href: `#/site/${s.site}` });
    }
    if (fl.NOT_IN_SITEMAP) {
      add({ tier: 2, site: s.site, title: `${plural(fl.NOT_IN_SITEMAP, "indexable page")} missing from the ${s.site} sitemaps`, minutes: 10,
        why: "Google finds pages much faster through the sitemap. Check the SEO plugin's sitemap settings for these.",
        cta: "See pages", href: `#/site/${s.site}?flag=NOT_IN_SITEMAP` });
    }
  }

  // ---- content (strategic): ranked by expected pages
  const CONTENT = {
    fix_trust: ["Source or remove unverified claims", "Phrasings that read as invented first-hand tests, ratings or statistics. Google weighs this across the whole site."],
    differentiate: ["Merge or differentiate near-duplicates", "Pages sharing most of their text with another page are what Google most often leaves crawled, not indexed. Check Bing clicks before merging."],
    expand: ["Add real substance to short pages", "The main content is short. Add first-hand, sourced detail, not filler."],
    add_links: ["Add internal links by hand", "No natural anchor was found automatically. Link to these from related pages Google already indexes."],
  };
  for (const s of live) {
    const pot = s.potential || {};
    const linked = new Set(S.st.links.filter((l) => l.site === s.site && ["proposed", "approved", "applied"].includes(l.status)).map((l) => l.target));
    for (const [action, [title, why]] of Object.entries(CONTENT)) {
      const p = pot[action];
      if (!p || !p.n) continue;
      const n = action === "add_links" ? Math.max(0, p.n - linked.size) : p.n;
      const gain = p.pages * (n / p.n);
      if (!n || gain < 0.2) continue;
      const flag = { fix_trust: "TRUST_RISK", differentiate: "NEAR_DUPLICATE" }[action];   // full lists; the plan holds the top pages
      add({ tier: 3, site: s.site, gain, minutes: n * MIN_PER_PAGE[action], title: `${title}: ${plural(n, "page")} on ${s.site}`, why,
        cta: flag ? "See pages" : "Top pages", href: flag ? `#/site/${s.site}?flag=${flag}` : `#/plan?site=${encodeURIComponent(s.site)}&action=${action}` });
    }
  }
  const perMin = (x) => (x.gain === null ? 0 : x.gain / Math.max(1, x.minutes));
  return out.sort((a, b) => a.tier - b.tier || (a.tier === 1 ? perMin(b) - perMin(a) : a.tier === 3 ? b.gain - a.gain : 0));
}

function priorityList(items, limit) {
  const ol = h("ol", { class: "prio" });
  const gainText = (g) => `≈ +${g < 1 ? g.toFixed(1) : Math.round(g)} ${g >= 0.95 && g < 1.5 ? "page" : "pages"} indexed`;
  const draw = (n) => ol.replaceChildren(...items.slice(0, n).map((p, i) => h("li", {},
    h("span", { class: `n t${p.tier}`, text: String(i + 1) }),
    h("div", { class: "body" },
      h("div", { class: "ttl", text: p.title }),
      h("div", { class: "muted small", text: p.why }),
      h("div", {},
        h("span", { class: `chip tier t${p.tier}`, text: TIER[p.tier] }),
        p.gain !== null ? h("span", { class: "chip gain", title: "Expected extra pages in Google's index (model estimate)", text: gainText(p.gain) }) : null,
        h("span", { class: "chip", text: `⏱ ${effortText(p.minutes)}` }))),
    h("a", { class: `btn small ${p.tier <= 1 ? "primary" : ""}`, href: p.href,
      ...(p.href.startsWith("http") ? { target: "_blank", rel: "noopener" } : {}) }, p.cta))));
  draw(limit);
  const more = items.length > limit
    ? h("button", { class: "btn small", onclick: (e) => { draw(items.length); e.target.remove(); } }, `Show all ${items.length}`) : null;
  return h("div", { class: "stack" }, ol, more);
}

function funnel(t) {
  const steps = [
    ["Found on your sites", t.urls, null],
    ["Indexable", t.indexable, `${fmt(t.urls - t.indexable)} blocked on purpose or by an error (noindex, redirect, canonical, 4xx/5xx), or not fetched yet`],
    ["Known to Google", t.knownToGoogle, `${fmt(t.unknown)} unknown to Google · ${fmt(t.nodata)} not inspected yet (2,000 checks a day per site)`],
    ["Crawled by Google", t.crawledByGoogle, `${fmt(t.discovered)} discovered but not crawled yet`],
    ["Indexed", t.indexed, `${fmt(t.crawled)} crawled but not indexed: quality, duplication or weak internal links`],
  ];
  const max = Math.max(1, t.urls);
  return h("div", { class: "funnel" }, ...steps.map(([label, v, lost], i) => h("div", { class: "step" },
    h("div", { class: "row small" }, h("b", { text: label }), h("span", { class: "spacer" }), h("span", { class: "num", text: fmt(v) }),
      i ? h("span", { class: "muted tiny", text: `${pct(v / Math.max(1, steps[i - 1][1]))} of previous` }) : null),
    h("div", { class: "fbar" }, h("span", { class: i === steps.length - 1 ? "ok" : null, style: { width: `${Math.max(0.5, (v / max) * 100)}%` } })),
    lost ? h("div", { class: "muted tiny", text: lost }) : null)));
}

function viewOverview() {
  if (!S.summary) return noData();
  const sites = S.summary.sites.map((s) => ({ s, n: siteNumbers(s) }));
  const t = sites.reduce((a, { n }) => {
    for (const k of Object.keys(n)) if (typeof n[k] === "number") a[k] = (a[k] || 0) + n[k];
    return a;
  }, {});
  t.indexable = sites.reduce((a, { s }) => a + Object.entries(s.counts || {}).reduce((x, [k, v]) => x + (BLOCKERS.has(k) || k === "NOT_CHECKED" ? 0 : v), 0), 0);
  t.knownToGoogle = t.indexed + t.crawled + t.discovered + t.stale;
  t.crawledByGoogle = t.indexed + t.crawled + t.stale;
  const lastRun = S.st.runs[0];
  const queued = S.st.commands.find((c) => c.status === "pending" || c.status === "running");
  const todo = priorities();
  const es = engineStatus();

  const kpi = (v, l, cls) => h("div", { class: "card kpi" }, h("div", { class: `v ${cls || ""}`, text: v }), h("div", { class: "l", text: l }));
  const head = h("div", { class: "row" }, h("h1", { text: "Portfolio" }), h("span", { class: "spacer" }),
    h("span", { class: "pill" }, h("span", { class: `dot ${es.cls}` }), `Engine: ${es.text}`),
    h("span", { class: "pill" }, h("span", { class: `dot ${lastRun && lastRun.status === "failed" ? "bad" : "ok"}` }),
      lastRun ? `Last run ${rel(lastRun.finished_at || lastRun.started_at)} · ${lastRun.status}` : "No runs yet"),
    queued ? h("span", { class: "pill" }, h("span", { class: "dot warn" }), `Run ${queued.status} (queued ${rel(queued.created_at)})`) : null,
    h("span", { class: "pill muted", text: `Data from ${rel(S.summary.generated_at)}` }));

  const quick = todo.filter((p) => p.tier <= 1);
  const quickGain = quick.reduce((g, p) => g + (p.gain || 0), 0);
  const quickMin = quick.reduce((m, p) => m + p.minutes, 0);
  const prioCard = h("div", { class: "card stack prio-card" },
    h("div", { class: "row" }, h("h2", { text: "Your priorities", style: { margin: "0" } }), h("span", { class: "spacer" }),
      quick.length ? h("span", { class: "muted small", text: `Urgent and quick wins: ${effortText(quickMin)} of your time, ≈ +${quickGain.toFixed(1)} pages indexed` }) : null),
    todo.length ? priorityList(todo, 8)
      : h("p", { class: "muted", text: "Nothing needs you right now. The engine keeps submitting, checking and watching every page." }),
    h("div", { class: "muted tiny", text: "Ranked: urgent problems first, then quick wins by expected pages per minute, then technical fixes, then content work by expected pages. " +
      "Submissions, inspections, re-checks and approved links run on their own; this list is only what needs you." }));

  const kpis = h("div", { class: "grid kpis" },
    kpi(fmt(t.urls), "URLs found on your sites"),
    kpi(fmt(t.indexed), `Indexed by Google${t.known ? ` (${pct(t.indexed / t.known)} of checked)` : ""}`, "good"),
    kpi(fmt(t.crawled), "Crawled, not indexed", "bad"),
    kpi(fmt(t.discovered + t.unknown), "Discovered or unknown"),
    kpi(fmt(t.conflicts), "In a sitemap but blocked", t.conflicts ? "warn" : ""),
    kpi(fmt(t.earners), "Pages earning Bing clicks"),
  );

  const imp = S.summary.impact || {};
  const rateTxt = (a, b) => (a ? `${b} of ${a} (${pct(b / a)})` : "not measured yet");
  const impact = h("div", { class: "card stack" }, h("h2", { text: `Last ${imp.days || 30} days` }),
    h("div", { class: "row" },
      h("div", { class: "kpi" }, h("div", { class: "v good", text: `+${fmt(imp.gained || 0)}` }), h("div", { class: "l", text: "entered Google's index" })),
      h("span", { class: "spacer" }),
      h("div", { class: "kpi" }, h("div", { class: "v bad", text: `−${fmt(imp.lost || 0)}` }), h("div", { class: "l", text: "dropped out" }))),
    h("div", { class: "small" }, h("b", { text: "Requests: " }), rateTxt((imp.requests || {}).measured, (imp.requests || {}).indexed), " indexed within 7+ days"),
    h("div", { class: "small" }, h("b", { text: "New links: " }), rateTxt((imp.links || {}).measured, (imp.links || {}).indexed), " targets indexed"),
    h("div", { class: "small" }, h("b", { text: "Bing: " }), rateTxt((imp.bing || {}).measured, (imp.bing || {}).crawled_or_shown), " submitted URLs crawled or shown"));

  const openAlerts = S.st.alerts.filter((a) => !a.acked);
  const alertCard = h("div", { class: "card stack" }, h("h2", {}, "Alerts ", openAlerts.length ? h("span", { class: "badge", text: String(openAlerts.length) }) : null),
    openAlerts.length ? h("ul", { class: "plain" }, ...[...openAlerts].sort((a, b) => ((({ high: 0, warn: 1, info: 2, good: 3 })[a.severity] ?? 2) - (({ high: 0, warn: 1, info: 2, good: 3 })[b.severity] ?? 2)) || (a.at < b.at ? 1 : -1)).slice(0, 4).map((a) => h("li", { class: "alert-row small" },
      h("span", { class: `sev ${a.severity}` }), h("span", {}, h("b", { text: `${a.site || ""} ` }), a.message))))
      : h("p", { class: "muted small", text: "Nothing needs you. The engine watches every page for regressions on each run." }),
    h("a", { href: "#/alerts", class: "small", text: "All alerts →" }));

  const acts = (S.summary.actions || []).slice(0, 5);
  const actCard = h("div", { class: "card stack" }, h("h2", { text: "Top pages to work on" }),
    acts.length ? h("ul", { class: "plain" }, ...acts.map((a) => h("li", { class: "small" },
      h("b", { text: ACTION_SHORT[a.action] || a.action }), " · ", h("a", { href: a.url, target: "_blank", rel: "noopener", text: a.title || a.url }),
      h("div", { class: "muted tiny" }, `${a.site} · odds `, oddsSpan(a.p_now), h("span", { class: "arrow", text: "→" }), oddsSpan(a.p_after)))))
      : h("p", { class: "muted small", text: "Appears after a run with Google data." }),
    h("a", { href: "#/plan", class: "small", text: "Full plan →" }));

  const lever = (site) => todo.find((p) => p.site === site);
  const short = (title, site) => title.replace(` on ${site}`, "").replace(` in the ${site} sitemaps`, " in its sitemaps").replace(` from the ${site} sitemaps`, " from its sitemaps");
  const table = h("table", {},
    h("thead", {}, h("tr", {}, ...["Site", "URLs", "States", "Indexed", "Share", "Crawled", "Unknown", "Next step"].map((x, i) =>
      h("th", { class: i && i !== 2 && i !== 7 ? "n" : null, text: x })))),
    h("tbody", {}, ...sites.map(({ s, n }) => {
      const lv = lever(s.site);
      return h("tr", { class: "click", onclick: () => (location.hash = `#/site/${s.site}`) },
        h("td", {}, h("a", { href: `#/site/${s.site}`, text: s.site }), isPaused(s.site) ? h("span", { class: "chip", text: " paused" }) : null),
        h("td", { class: "n", text: fmt(n.urls) }),
        h("td", {}, bucketBar(s.counts || {})),
        h("td", { class: "n good", text: fmt(n.indexed) }),
        h("td", { class: "n", text: pct(n.share) }),
        h("td", { class: "n", text: fmt(n.crawled) }),
        h("td", { class: "n", text: fmt(n.unknown) }),
        h("td", { class: "small lever" }, lv ? h("a", { href: lv.href, onclick: (e) => e.stopPropagation(), text: short(lv.title, s.site),
          ...(lv.href.startsWith("http") ? { target: "_blank", rel: "noopener" } : {}) })
          : h("span", { class: "muted", text: isPaused(s.site) ? "paused" : "nothing pending" })));
    })));

  return h("div", { class: "stack" }, head, prioCard, kpis,
    h("div", { class: "cols" },
      h("div", { class: "card" }, h("h2", { text: "Google indexed share of checked URLs" }), trendChart()),
      h("div", { class: "card" }, h("h2", { text: "Where pages get stuck" }), funnel(t))),
    h("div", { class: "grid kpis", style: { gridTemplateColumns: "repeat(auto-fit, minmax(280px, 1fr))", margin: "0" } }, actCard, impact, alertCard),
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

async function viewSite(site, params) {
  const meta = S.summary && S.summary.sites.find((s) => s.site === site);
  if (!meta) return h("div", { class: "card empty", text: `No data for ${site} yet.` });
  const data = await loadSite(site);
  if (!data) return h("div", { class: "card empty", text: `No URL data for ${site} yet.` });
  const v = (S.view[site] ||= { bucket: "", flag: "", q: "", ignored: false, page: 0, open: null });
  preset(v, params, ["bucket", "flag"]);
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
      toast({ note: "Note saved", ignore: "Ignored: hidden from the work lists and never submitted", unignore: "No longer ignored",
        requested: "Marked as requested", unrequested: "Request mark removed" }[action] || "Saved");
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

function viewPlan(params) {
  if (!S.summary) return noData();
  const m = S.summary.model || {};
  const acts = S.summary.actions || [];
  const v = (S.view.__plan ||= { site: "", action: "" });
  preset(v, params, ["site", "action"]);
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
  preset(v, params, ["site", "tab"]);
  const target = params.get("target");
  const groups = { proposed: ["proposed"], approved: ["approved", "undo"], applied: ["applied"], other: ["rejected", "failed", "undone"] };
  const all = S.st.links.filter((l) => (!v.site || l.site === v.site) && (!target || l.target === target));
  const shown = all.filter((l) => groups[v.tab].includes(l.status));
  if (v.tab === "approved") shown.sort((a, b) => (a.updated_at < b.updated_at ? -1 : 1));    // the order the engine works in
  const seen = setting("engine_last_seen", null);
  const cap = Number(setting("links_per_site_per_day", 5));
  const paused = setting("submit_paused", false);
  const pausedSites = new Set(setting("sites_disabled", []));
  const autopilot = setting("link_autopilot", false);
  const es = engineStatus();
  const pcNote = es.cls === "bad" ? "your PC is offline, so it happens when it is switched on"
    : seen ? `your PC (last seen ${rel(seen.at)}) checks every 10 minutes` : "your PC checks every 10 minutes";

  // What the engine will do with approved links: per site, `cap` in any 24 hours, oldest approval first.
  const slotsOf = () => {
    const out = {};
    for (const site of new Set(S.st.links.map((l) => l.site))) {
      const used = S.st.links.filter((l) => l.site === site && l.status === "applied" && l.applied_at && daysSince(l.applied_at) < 1);
      const approved = S.st.links.filter((l) => l.site === site && l.status === "approved").sort((a, b) => (a.updated_at < b.updated_at ? -1 : 1));
      const free = Math.max(0, cap - used.length);
      const nextFree = used.map((l) => new Date(l.applied_at).getTime() + 86400000).sort((a, b) => a - b)[0];
      out[site] = { used: used.length, free, approved: approved.length, next: new Set(approved.slice(0, free).map((l) => l.id)),
        queued: Math.max(0, approved.length - free), nextFree };
    }
    return out;
  };
  const blocked = (site) => (paused ? "everything is paused in Settings" : pausedSites.has(site) ? "this site is paused in Settings" : null);

  const act = async (ids, action, btn) => {
    const label = btn ? btn.textContent : "";
    if (btn) { btn.disabled = true; btn.textContent = { approve: "Approving…", undo: "Queuing undo…" }[action] || "Saving…"; }
    try {
      const r = await api.post("/api/link", { ids, action });
      if (r.changed) for (const l of S.st.links) if (ids.includes(l.id)) l.status = r.status;
      let msg;
      if (!r.changed) msg = "Nothing changed: the list was out of date, so it has been refreshed";
      else if (action === "approve") {
        const slots = slotsOf();
        const sites = [...new Set(S.st.links.filter((l) => ids.includes(l.id)).map((l) => l.site))];
        const why = sites.map(blocked).find(Boolean);
        const now = S.st.links.filter((l) => ids.includes(l.id) && l.status === "approved");
        const soon = now.filter((l) => slots[l.site].next.has(l.id)).length;
        msg = why ? `Approved ${r.changed}, but ${why}: nothing is applied until you change that.`
          : `Approved ${r.changed}. ${soon ? `${soon} will be added soon` : "None fits today's limit"}` +
            `${now.length - soon > 0 ? `${soon ? ", " : ": "}${now.length - soon} wait for a free slot (${cap} per site per 24 hours)` : ""}; ${pcNote}.`;
      } else if (action === "undo") msg = `Undo queued for ${r.changed}: ${pcNote}.`;
      else msg = { cancel: "Moved back to Proposed", reject: `Rejected ${r.changed}`, reopen: "Back in Proposed" }[action] || `${r.changed}: ${r.status}`;
      toast(msg, 7000);
      if (!r.changed) await loadAll();
      renderChrome();
      render();
    } catch (e) {
      if (btn) { btn.disabled = false; btn.textContent = label; }
      toast(`Not saved: ${e.message}`, 6000);
    }
  };

  const live = (l) => /visible to visitors/.test(l.result || "");
  const seeIt = (l) => `${l.source}${l.source.includes("?") ? "&" : "?"}nc=${Date.now().toString(36)}#:~:text=${encodeURIComponent(decodeEntities(l.anchor))}`;
  const slots = slotsOf();
  const state = (l) => {
    if (l.status === "applied") return live(l) ? ["ok", "live for visitors"] : ["warn", "in WordPress, cache refreshing"];
    if (l.status === "approved" || l.status === "undo") {
      const why = blocked(l.site);
      if (why) return ["bad", `on hold: ${why}`];
      if (l.status === "undo") return ["warn", "undo waiting for your PC"];
      return slots[l.site].next.has(l.id) ? ["warn", "next: your PC adds it soon"] : ["warn", "queued: waits for a free slot"];
    }
    return l.status === "failed" ? ["bad", "failed"] : [null, l.status];
  };
  const card = (l) => {
    const [cls, txt] = state(l);
    return h("div", { class: "card stack" },
      h("div", { class: "row small" }, h("span", { class: "chip", text: l.site }),
        h("span", { class: "pill" }, cls ? h("span", { class: `dot ${cls}` }) : null, txt),
        l.applied_at ? h("span", { class: "muted tiny", text: `applied ${rel(l.applied_at)}` }) : null,
        l.gain ? h("span", { class: "muted tiny", text: `expected gain ${(+l.gain).toFixed(2)}` }) : null),
      h("div", {}, "On ", h("a", { href: l.source, target: "_blank", rel: "noopener", text: l.source_title || l.source }),
        h("span", { class: "arrow", text: " link to " }), h("a", { href: l.target, target: "_blank", rel: "noopener", text: l.target_title || l.target })),
      h("div", { class: "small muted" }, "“", ...markIn(decodeEntities(l.context), decodeEntities(l.anchor)), "”"),
      l.result ? h("div", { class: `tiny ${l.status === "failed" ? "bad" : "muted"}`, text: l.result }) : null,
      h("div", { class: "row" },
        l.status === "proposed" ? [h("button", { class: "btn small primary", onclick: (e) => act([l.id], "approve", e.currentTarget) }, "Approve"),
          h("button", { class: "btn small", onclick: (e) => act([l.id], "reject", e.currentTarget) }, "Reject")] : null,
        l.status === "approved" ? h("button", { class: "btn small", onclick: (e) => act([l.id], "cancel", e.currentTarget) }, "Cancel") : null,
        l.status === "applied" ? [h("a", { class: "btn small", href: seeIt(l), target: "_blank", rel: "noopener",
          title: "Opens the page past the cache, scrolled to the linked words" }, "See it on the page"),
          h("button", { class: "btn small", onclick: (e) => act([l.id], "undo", e.currentTarget) }, "Undo")] : null,
        ["rejected", "failed", "undone"].includes(l.status) ? h("button", { class: "btn small", onclick: (e) => act([l.id], "reopen", e.currentTarget) }, "Reopen") : null));
  };

  // One plain sentence per site about what will happen to its approved links.
  const queueNotes = Object.entries(slots).filter(([site, s]) => s.approved && (!v.site || site === v.site)).map(([site, s]) => {
    const why = blocked(site);
    const when = s.nextFree ? new Date(s.nextFree).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }) : null;
    return h("div", {}, h("b", { text: `${site}: ` }), why ? `${s.approved} approved, on hold because ${why}.`
      : s.free ? `${Math.min(s.free, s.approved)} go out soon${s.queued ? `, ${s.queued} more follow as slots free up` : ""} (${cap} per site per 24 hours, ${s.used} used).`
        : `today's limit is reached (${s.used} of ${cap} added in the last 24 hours): ${s.approved} approved wait${when ? `; the next slot frees about ${when}` : ""}.`);
  });
  const noPurge = S.st.links.some((l) => l.status === "applied" && !live(l) && /cached copy/.test(l.result || "") && !/purge: purged/.test(l.result || ""));
  const hold = paused ? "Everything is paused in Settings: approved links are not applied until you resume."
    : pausedSites.size ? `Paused sites get no link changes: ${[...pausedSites].join(", ")}.` : null;
  return h("div", { class: "stack" }, h("h1", { text: "Internal links" }),
    h("div", { class: "card small stack" },
      h("div", {}, "Links from pages Google already indexes are the strongest lever for pages Google knows but won't index. Each proposal links a phrase that ",
        h("b", { text: "already exists" }), " in the source page's text; nothing else on the page changes."),
      h("div", {}, `Approve a link and ${pcNote}. It adds at most ${cap} per site in any 24 hours, with a backup, a check of the page and Undo. `,
        `Autopilot is ${autopilot ? "ON (it also applies the best proposals itself, except on gearuptofit)" : "off (only links you approve are applied)"}. `,
        h("a", { href: "#/settings", text: "Settings" }))),
    hold ? h("div", { class: "card small note" }, h("b", { text: "On hold. " }), hold, " ", h("a", { href: "#/settings", text: "Open Settings" })) : null,
    queueNotes.length ? h("div", { class: "card small note stack" }, h("b", { text: "What happens to approved links" }), ...queueNotes) : null,
    noPurge ? h("div", { class: "card small note stack" },
      h("div", {}, h("b", { text: "Links are in WordPress, but Cloudflare keeps serving the old copy of those pages for up to 4 hours. " }),
        "Visitors and Google see a link once the cache refreshes; each card says when. To make links show at once, let the engine clear the cache of just that page:"),
      h("ol", { class: "tight" },
        h("li", {}, "Cloudflare dashboard → My Profile → API Tokens."),
        h("li", {}, "Edit the token you made for the Index Engine."),
        h("li", {}, "Permissions → Add: Zone · Cache Purge · Purge. Zone resources: All zones from the account."),
        h("li", {}, "Continue to summary → Update token. Nothing else to change: the engine picks it up on the next link."))) : null,
    target ? h("div", { class: "row small" }, h("span", { text: `Showing links to ${target}` }), h("a", { href: "#/links", text: "show all" })) : null,
    h("div", { class: "row" },
      h("div", { class: "seg-ctl" }, ...Object.keys(groups).map((k) => h("button", { type: "button", class: v.tab === k ? "on" : null,
        onclick: () => { v.tab = k; render(); } }, `${k[0].toUpperCase()}${k.slice(1)} (${all.filter((l) => groups[k].includes(l.status)).length})`))),
      h("select", { "aria-label": "Site", onchange: (e) => { v.site = e.target.value; render(); } },
        h("option", { value: "", text: "All sites" }), ...[...new Set(S.st.links.map((l) => l.site))].map((s) => h("option", { value: s, selected: v.site === s, text: s }))),
      h("span", { class: "spacer" }),
      v.tab === "proposed" && shown.length ? h("button", { class: "btn small", title: `Your PC still adds at most ${cap} per site per 24 hours`,
        onclick: (e) => act(shown.slice(0, 100).map((l) => l.id), "approve", e.currentTarget) },
        `Approve all ${Math.min(100, shown.length)} shown`) : null),
    shown.length ? shown.map(card) : h("div", { class: "card empty", text: v.tab === "proposed" ? "No proposals waiting. New ones appear after the daily run." : "Nothing here." }));
}

function viewAlerts() {
  const ack = async (body) => { try { await api.post("/api/alert", body); await loadAll(); render(); } catch (e) { toast(e.message); } };
  const open = S.st.alerts.filter((a) => !a.acked);
  const urgency = { high: 0, warn: 1, info: 2, good: 3 };
  const ordered = [...S.st.alerts].sort((a, b) => (a.acked - b.acked) || ((urgency[a.severity] ?? 2) - (urgency[b.severity] ?? 2)) || (a.at < b.at ? 1 : -1));
  return h("div", { class: "stack" }, h("div", { class: "row" }, h("h1", { text: "Alerts" }), h("span", { class: "spacer" }),
    open.length ? h("button", { class: "btn small", onclick: () => ack({ all: true }) }, "Acknowledge all") : null),
    h("p", { class: "muted small", text: "Raised automatically: an indexable page that turns noindex, 404s, redirects or loses its canonical; content that shrinks; a sitemap that drops URLs; a site whose pages stop answering; pages entering or leaving Google's index." }),
    S.st.alerts.length ? ordered.map((a) => h("div", { class: `card alert-row ${a.acked ? "acked" : ""}` },
      h("span", { class: `sev ${a.severity}`, title: a.severity }),
      h("div", { style: { flex: "1", minWidth: "0" } }, h("div", {}, h("b", { text: `${a.site || "portfolio"} · ` }), a.message),
        a.url ? h("a", { class: "tiny", href: a.url, target: "_blank", rel: "noopener", text: a.url }) : null,
        h("div", { class: "muted tiny", text: rel(a.at) })),
      a.acked ? null : h("button", { class: "btn small", onclick: () => ack({ id: a.id }) }, "OK")))
      : h("div", { class: "card empty", text: "No alerts yet." }));
}

function viewRequests(params) {
  if (!S.summary) return noData();
  const only = params.get("site");
  const open = openRequests().filter((r) => !only || r.site === only);
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
    only ? h("div", { class: "row small" }, h("span", { text: `Showing ${only}` }), h("a", { href: "#/requests", text: "show all sites" })) : null,
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
    h("div", { class: "card stack" }, h("h2", { text: "Sites" }), h("div", { class: "grid sites-grid" }, ...checks),
      h("div", { class: "muted tiny", text: "Unticked sites are skipped by every run, link edit and fresh-content check, even when you pick one in Run now." })),
    h("div", { class: "card stack" }, h("h2", { text: "Engine" }),
      h("label", { class: "chk" }, paused, "Pause everything that changes something: submissions (Bing, IndexNow, sitemaps) and link edits on your sites"),
      h("div", { class: "muted tiny", text: "While paused the engine only measures. Approved links wait; Undo waits too." }),
      field("Daily run time on your PC", time, "The engine checks every 10 minutes and runs once a day after this time. Google's inspection quota resets at midnight Pacific (10:00 in Greece)."),
      field("Request-indexing list size per site", queue, "Search Console allows roughly 10 manual requests a day per property."),
      field("Max URL inspections per site per run", inspect, "Leave empty to use the full daily quota (about 1,950)."),
      h("label", { class: "chk" }, freshChk, "Fresh-content autopilot: every 10 minutes, find new or updated posts and get them audited, inspected and submitted within minutes"),
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
    if (route === "site" && arg) node = await viewSite(decodeURIComponent(arg), params);
    else if (route === "plan") node = viewPlan(params);
    else if (route === "links") node = viewLinks(params);
    else if (route === "alerts") node = viewAlerts();
    else if (route === "requests") node = viewRequests(params);
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
    toast(r.note || "Run queued. Your PC picks it up within 10 minutes.");
    await loadAll();
    render();
  } catch (err) { toast(err.message); }
});
document.getElementById("logout").addEventListener("click", async () => {
  await api.post("/api/auth/logout", {}).catch(() => {});
  location.replace("/login");
});
window.addEventListener("hashchange", render);
const signature = () => JSON.stringify([S.st && S.st.links.map((l) => `${l.id}${l.status}${(l.result || "").length}`).join(","),
  S.st && S.st.commands.map((c) => `${c.id}${c.status}`).join(","), S.st && S.st.runs.slice(0, 3).map((r) => `${r.id}${r.status}`).join(","),
  S.st && S.st.alerts.filter((a) => !a.acked).length, S.st && S.st.fixes.map((f) => f.status).join(""), S.summary && S.summary.generated_at,
  S.st && S.st.settings.engine_last_seen && S.st.settings.engine_last_seen.updated_at]);
// Every 2 minutes: reload, and redraw the page only when something changed and you are not in the middle of typing.
setInterval(async () => {
  if (document.hidden) return;
  const before = S.st ? signature() : "";
  await loadAll().catch(() => {});
  const route = location.hash.replace(/^#\/?/, "").split(/[/?]/)[0];
  const busy = document.querySelector("dialog[open]") || /^(INPUT|TEXTAREA|SELECT)$/.test((document.activeElement || {}).tagName || "");
  if (signature() !== before && !busy && route !== "settings") render();
}, 120000);

loadAll().then(render).catch((e) => {
  document.getElementById("app").replaceChildren(h("div", { class: "card empty", text: `Could not load: ${e.message}` }));
});
