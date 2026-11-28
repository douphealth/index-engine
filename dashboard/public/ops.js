// Prune queue, crawler telemetry and the randomized experiment: three views on top of app.js's helpers (h, S, api,
// toast, fmt, pct, setting, loadSite, render). Loaded before app.js; nothing here runs until app.js renders a page.

/* ------------------------------------------------------------------ prune queue */
const PRUNE_STATUS = { proposed: "Waiting for you", approved: "Approved: waiting for your PC", applied: "Applied", failed: "Failed",
  rejected: "Rejected", undo: "Undo queued", undone: "Undone" };
const PRUNE_EXCLUDED = {
  "gearuptofit.com": "has its own owner-run prune in the child theme and a release script; the engine leaves it to that.",
  "plantastichaven.com": "has no Code Snippets plugin, so the helper that sets the rules cannot be installed.",
};
const GOOGLE_STATE = { INDEXED: "indexed", CRAWLED_NOT_INDEXED: "crawled, not indexed", DISCOVERED_NOT_INDEXED: "discovered, not crawled",
  UNKNOWN_TO_GOOGLE: "unknown to Google", GOOGLE_PICKED_OTHER: "Google picked another URL" };

async function pruneAct(ids, action, btn) {
  if (btn) { btn.disabled = true; btn.textContent = "Saving…"; }
  let changed = 0, status = null;
  try {
    for (let i = 0; i < ids.length; i += 200) {
      const r = await api.post("/api/prune", { ids: ids.slice(i, i + 200), action });
      changed += r.changed || 0; status = r.status;
    }
    if (changed) for (const p of S.st.prunes || []) if (ids.includes(p.id)) p.status = status;
    const cap = Number(setting("prunes_per_site_per_day", 25));
    toast(!changed ? "Nothing changed: the list was out of date" : action === "approve"
      ? `Approved ${fmt(changed)}. Your PC applies up to ${cap} per site a day, within 10 minutes when it is on.`
      : action === "undo" ? `Undo queued for ${fmt(changed)}.` : `${fmt(changed)}: ${PRUNE_STATUS[status] || status}`, 7000);
    if (!changed) await loadAll();
    renderChrome(); render();
  } catch (e) { if (btn) btn.disabled = false; toast(`Not saved: ${e.message}`, 6000); }
}

function viewPrune(params) {
  const all = S.st.prunes || [];
  const sites = [...new Set(all.map((p) => p.site))].sort();
  const v = (S.view.__prune ||= { status: "proposed", site: params.get("site") || "" });
  if (params.get("site") !== null) v.site = params.get("site");
  const mine = all.filter((p) => !v.site || p.site === v.site);
  const counts = mine.reduce((m, p) => ((m[p.status] = (m[p.status] || 0) + 1), m), {});
  const status = counts[v.status] ? v.status : (Object.keys(PRUNE_STATUS).find((k) => counts[k]) || "proposed");
  const items = mine.filter((p) => p.status === status);
  const cap = Number(setting("prunes_per_site_per_day", 25));
  const perSite = sites.map((s) => {
    const n = all.filter((p) => p.site === s);
    return { site: s, open: n.filter((p) => p.status === "proposed").length, applied: n.filter((p) => p.status === "applied").length };
  });

  const picker = h("select", { onchange: (e) => { v.site = e.target.value; location.hash = v.site ? `#/prune?site=${encodeURIComponent(v.site)}` : "#/prune"; } },
    h("option", { value: "", text: `All websites (${fmt(all.length)})` }),
    ...perSite.map((x) => h("option", { value: x.site, selected: x.site === v.site, text: `${x.site} · ${fmt(x.open)} waiting` })));
  const tabs = h("div", { class: "row" }, ...Object.keys(PRUNE_STATUS).filter((k) => counts[k]).map((k) =>
    h("button", { class: `btn small ${k === status ? "primary" : ""}`, onclick: () => { v.status = k; render(); } }, `${PRUNE_STATUS[k].split(":")[0]} (${fmt(counts[k])})`)),
    h("span", { class: "spacer" }),
    status === "proposed" && items.length > 1 ? h("button", { class: "btn small", onclick: (e) => {
      if (confirm(`Approve all ${items.length} pages${v.site ? ` on ${v.site}` : " on every website"}? Each one is applied with a live check and can be undone.`))
        pruneAct(items.map((p) => p.id), "approve", e.target);
    } }, `Approve all ${fmt(items.length)}`) : null);

  const SHOW = 200;
  const btnsFor = (p) => ({
    proposed: [["approve", "Approve", "primary"], ["reject", "Keep the page", ""]], approved: [["cancel", "Cancel", ""]],
    failed: [["approve", "Retry", ""], ["reject", "Keep the page", ""]], applied: [["undo", "Undo", ""]],
    rejected: [["reopen", "Reopen", ""]], undone: [["reopen", "Reopen", ""]],
  }[p.status] || []);
  const rows = items.slice(0, SHOW).map((p) => {
    const ev = p.evidence || {};
    return h("tr", {},
      h("td", { class: "page wide" }, httpUrl(p.url) ? h("a", { class: "clamp2", href: p.url, target: "_blank", rel: "noopener", text: decodeEntities(p.title || "") || shortPath(p.url) }) : h("span", { text: p.url }),
        h("div", { class: "u", text: `${v.site ? "" : p.site + " · "}${shortPath(p.url)}` })),
      h("td", {}, h("span", { class: `chip ${p.action === "redirect" ? "tier t1" : ""}`, text: p.action === "redirect" ? "301 redirect" : "noindex" }),
        p.action === "redirect" && p.target ? h("div", { class: "tiny muted", text: `to ${shortPath(p.target)}` }) : null),
      h("td", { class: "n", text: fmt((ev.google_impr_90d || 0) + (ev.bing_impr || 0)) }),
      h("td", { class: "n", text: fmt(ev.age_days) }),
      h("td", { class: "small", text: GOOGLE_STATE[ev.bucket] || (ev.bucket || "").toLowerCase().replace(/_/g, " ") }),
      h("td", { class: "small", title: p.why || "" }, ...String(ev.flags || "").split(",").filter((f) => f && f !== "BING_EARNER").slice(0, 4)
        .map((f) => h("span", { class: "chip", text: f.toLowerCase().replace(/_/g, " ") })),
        ev.inbound_links === 0 ? h("span", { class: "chip", text: "no inbound links" }) : null,
        p.result ? h("div", { class: `tiny ${p.status === "failed" ? "bad" : "muted"}`, text: p.result }) : null),
      h("td", { class: "actions" }, ...btnsFor(p).map(([a, label, cls]) => h("button", { class: `btn small ${cls}`, onclick: (e) => pruneAct([p.id], a, e.target) }, label))));
  });

  const excluded = Object.entries(PRUNE_EXCLUDED).map(([s, why]) => h("li", {}, h("b", { text: s }), ` ${why}`));
  return h("div", { class: "stack" },
    h("div", { class: "page-head" }, h("h1", { text: "Prune queue" }), picker),
    h("p", { class: "muted", text: "Pages that earn nothing on either engine, from the prune plan, cut down to the clear-cut cases: 0 clicks and under 20 impressions on Google and Bing in 90 days, at least 120 days old, and no other site linking to them. " +
      "A page other sites link to gets a 301 to the closest page Google shows instead. Nothing happens until you approve it." }),
    h("div", { class: "grid kpis" },
      h("div", { class: "kpi" }, h("div", { class: "v", text: fmt(all.filter((p) => p.status === "proposed").length) }), h("div", { class: "l", text: "waiting for your decision" })),
      h("div", { class: "kpi" }, h("div", { class: "v", text: fmt(all.filter((p) => p.status === "applied").length) }), h("div", { class: "l", text: "applied and verified on the live page" })),
      h("div", { class: "kpi" }, h("div", { class: "v", text: String(cap) }), h("div", { class: "l", text: "applied per site per day at most (Settings)" }))),
    h("details", { class: "card" }, h("summary", { text: "How a prune is applied, and how to undo it" }),
      h("ul", { class: "small" },
        h("li", { text: "The first approval on a website installs one small code snippet there, “Index Engine helper”, through the Code Snippets plugin. It keeps every rule in one WordPress option and does nothing else. The engine checks the home page still answers afterwards and switches the snippet off again if it does not." }),
        h("li", { text: "noindex is served as “noindex, follow”: Google drops the page but still follows its links, and the page leaves the sitemap. The post itself is not edited." }),
        h("li", { text: "Each rule is checked on the live page (a fresh copy and the cached one). A rule the page does not serve is removed again and reported as failed." }),
        h("li", { text: "Undo removes the rule. Switching the snippet off in WordPress (Snippets → Index Engine helper) removes every rule at once." })),
      h("div", { class: "small muted" }, "Left out: ", h("ul", { class: "small" }, ...excluded))),
    tabs,
    items.length ? h("div", { class: "tablewrap" }, h("table", {},
      h("thead", {}, h("tr", {}, ...["Page", "Action", "Impressions, 90 days", "Days old", "Google", "Signals (hover for the full reason)", ""].map((x, i) => h("th", { class: i === 2 || i === 3 ? "n" : null, text: x })))),
      h("tbody", {}, ...rows)))
      : h("div", { class: "card empty", text: all.length ? "Nothing in this list." : "No prune proposals yet. They appear after the next full run." }),
    items.length > SHOW ? h("div", { class: "small muted", text: `Showing ${SHOW} of ${fmt(items.length)}. “Approve all” covers every one.` }) : null);
}

/* ------------------------------------------------------------------ crawler telemetry */
const crawlBad = (st) => Object.entries(st || {}).reduce((a, [k, n]) => a + ((+k >= 500 || +k === 429) ? n : 0), 0);
const crawlGone = (st) => Object.entries(st || {}).reduce((a, [k, n]) => a + ((+k === 404 || +k === 410) ? n : 0), 0);

function crawlChart(series) {
  if (!series || !series.length) return null;
  const W = 720, H = 150, pad = 26, max = Math.max(1, ...series.map((d) => Math.max(d[1], d[3])));
  const bw = (W - pad * 2) / series.length;
  const y = (v) => H - pad - (v / max) * (H - pad * 1.6);
  const bars = series.map((d, i) => {
    const x = pad + i * bw;
    return svg("g", {},
      svg("rect", { x: x + 1, y: y(d[1]), width: Math.max(1, bw - 2), height: H - pad - y(d[1]), class: "bar-g", rx: 2 }, svg("title", {}, `${d[0]}: Googlebot ${d[1]}, errors ${d[2]}, Bingbot ${d[3]}`)),
      d[2] ? svg("rect", { x: x + 1, y: y(d[2]), width: Math.max(1, bw - 2), height: H - pad - y(d[2]), class: "bar-bad", rx: 2 }) : null);
  });
  const line = series.map((d, i) => `${i ? "L" : "M"}${(pad + i * bw + bw / 2).toFixed(1)},${y(d[3]).toFixed(1)}`).join("");
  return h("div", { class: "chart-wrap" }, svg("svg", { class: "chart", viewBox: `0 0 ${W} ${H}`, role: "img", "aria-label": "Crawler fetches per day" },
    svg("line", { x1: pad, x2: W - pad, y1: H - pad, y2: H - pad, class: "grid-line" }),
    svg("text", { x: pad, y: 12 }, `${fmt(max)} a day`), ...bars, svg("path", { d: line, class: "l2" }),
    svg("text", { x: pad, y: H - 8 }, series[0][0]), svg("text", { x: W - pad, y: H - 8, "text-anchor": "end" }, series[series.length - 1][0])),
    h("div", { class: "legend small muted" }, h("span", { class: "sw g" }), "Googlebot  ", h("span", { class: "sw bad" }), "server errors (5xx, 429)  ", h("span", { class: "sw b" }), "Bingbot"));
}

async function viewCrawl(params) {
  if (!S.summary) return noData();
  const site = params.get("site") || "";
  const rows = (S.summary.sites || []).filter((s) => s.crawl).sort((a, b) => (b.crawl.googlebot_7d || 0) - (a.crawl.googlebot_7d || 0));
  const head = h("div", { class: "page-head" }, h("h1", { text: "Crawlers" }),
    h("select", { onchange: (e) => { location.hash = e.target.value ? `#/crawl?site=${encodeURIComponent(e.target.value)}` : "#/crawl"; } },
      h("option", { value: "", text: "All websites" }), ...rows.map((s) => h("option", { value: s.site, selected: s.site === site, text: s.site }))));
  const intro = h("p", { class: "muted", text: "What Googlebot and Bingbot fetched, day by day, from Cloudflare's own analytics (read-only; nothing runs in front of your sites). " +
    "It shows within a day whether Google came back after a change, and catches a site that answers crawlers with errors before Search Console does. " +
    "Only requests from the addresses Google and Bing publish for their crawlers count; anything else that calls itself Googlebot (your own checking tools, scrapers) is counted apart. " +
    "Cloudflare samples busy sites, so the numbers are close estimates. Days are UTC." });
  const after = crawlAfterCard();
  if (!site) {
    if (!rows.length) return h("div", { class: "stack" }, head, intro, h("div", { class: "card empty", text: "No crawler data yet. It is read on the next full run (needs the Cloudflare analytics token)." }));
    const tr = rows.map((s) => {
      const c = s.crawl, bad = crawlBad(c.status_7d), gone = crawlGone(c.status_7d), n = c.googlebot_7d || 0;
      const share = (x) => (n ? `${Math.round((100 * x) / n)}%` : "–");
      return h("tr", { class: "click", onclick: () => { location.hash = `#/crawl?site=${encodeURIComponent(s.site)}`; } },
        h("td", { class: "page" }, h("b", { text: s.site })),
        h("td", { class: "n", text: fmt(n) }), h("td", { class: "n", text: fmt(c.bingbot_7d) }),
        h("td", { class: `n ${bad / Math.max(1, n) >= 0.1 ? "bad" : ""}`, text: share(bad) }),
        h("td", { class: `n ${gone / Math.max(1, n) >= 0.15 ? "warn" : ""}`, text: share(gone) }),
        h("td", { class: "n", text: `${fmt(c.pages_fetched_30d)} of ${fmt(c.pages_known)}` }), h("td", { class: "small muted", text: c.last_day }));
    });
    return h("div", { class: "stack" }, head, intro, after,
      h("div", { class: "tablewrap" }, h("table", {},
        h("thead", {}, h("tr", {}, ...["Website", "Googlebot, 7 days", "Bingbot, 7 days", "Server errors", "Missing pages (404)", "Pages Google fetched, 30 days", "Last day"]
          .map((x, i) => h("th", { class: i ? "n" : null, text: x })))),
        h("tbody", {}, ...tr))));
  }
  const data = await loadSite(site);
  const c = data.crawl;
  if (!c) return h("div", { class: "stack" }, head, intro, h("div", { class: "card empty", text: `No crawler data for ${site}: its DNS is not on a Cloudflare account the analytics token can read.` }));
  const col = Object.fromEntries((data.cols || []).map((k, i) => [k, i]));
  const urls = (data.rows || []).map((r) => ({ url: r[col.url], title: r[col.title], bucket: r[col.bucket], value: +r[col.value] || 0,
    g30: +r[col.googlebot_30d] || 0, glast: r[col.googlebot_last] || "" }));
  const blockers = new Set(["FETCH_FAILED", "BROKEN", "SERVER_ERROR", "REDIRECTS", "NOINDEX", "ROBOTS_BLOCKED", "CANONICAL_ELSEWHERE", "NOT_HTML"]);
  const unseen = urls.filter((u) => !u.g30 && !blockers.has(u.bucket)).sort((a, b) => b.value - a.value);
  const n = c.googlebot_7d || 0, bad = crawlBad(c.status_7d), gone = crawlGone(c.status_7d);
  const fake = c.impostors_7d || {}, fakeN = (fake.google || 0) + (fake.bing || 0);
  const kinds = Object.entries(c.missing_7d || {});
  const moved = (c.errors_7d || []).filter((e) => e.moved_to);
  const missingCard = kinds.length ? h("div", { class: "card stack" }, h("h2", { text: "Missing addresses Googlebot asked for, by kind (7 days)" }),
    h("div", { class: "tablewrap" }, h("table", {}, h("thead", {}, h("tr", {}, h("th", { text: "Kind" }), h("th", { class: "n", text: "Fetches" }), h("th", { class: "n", text: "Addresses" }))),
      h("tbody", {}, ...kinds.map(([k, v]) => h("tr", {}, h("td", { text: k }), h("td", { class: "n", text: fmt(v.hits) }), h("td", { class: "n", text: fmt(v.paths) })))))),
    h("p", { class: "small muted", text: "Google keeps re-checking addresses it once knew for months after they are removed. That is normal and does not hurt rankings. " +
      "What is worth fixing: a page that only moved (a 301 to its new address keeps its history and links), a file the live pages still use, and a link on your own pages that points at a missing address." }),
    moved.length ? h("p", { class: "small", text: `${fmt(moved.length)} of the most-fetched missing pages only moved; ${fmt(moved.filter((e) => !e.moved_noindex).length)} of them to a page that can be indexed. Their new addresses are in the list below.` }) : null) : null;
  return h("div", { class: "stack" }, head, intro,
    h("div", { class: "grid kpis" },
      h("div", { class: "kpi" }, h("div", { class: "v", text: fmt(n) }), h("div", { class: "l", text: "Googlebot fetches in 7 days" })),
      h("div", { class: "kpi" }, h("div", { class: "v", text: fmt(c.bingbot_7d) }), h("div", { class: "l", text: "Bingbot fetches in 7 days" })),
      h("div", { class: "kpi" }, h("div", { class: `v ${n && bad / n >= 0.1 ? "bad" : ""}`, text: n ? pct(bad / n) : "–" }), h("div", { class: "l", text: "answered with a server error (5xx, 429)" })),
      h("div", { class: "kpi" }, h("div", { class: `v ${n && gone / n >= 0.15 ? "warn" : ""}`, text: n ? pct(gone / n) : "–" }), h("div", { class: "l", text: "were addresses that do not exist (404, 410)" })),
      h("div", { class: "kpi" }, h("div", { class: "v", text: fmt(fakeN) }), h("div", { class: "l", text: "requests that called themselves Googlebot or Bingbot from other addresses (not counted)" }))),
    h("div", { class: "card stack" }, h("h2", { text: "Fetches per day" }), crawlChart(data.crawl.series),
      h("div", { class: "row" }, ...Object.entries(c.status_7d || {}).sort((a, b) => b[1] - a[1]).map(([k, v]) => h("span", { class: `chip ${+k >= 500 || +k === 429 ? "bad" : +k >= 400 ? "warn" : ""}`, text: `${k}: ${fmt(v)}` })))),
    missingCard,
    (c.errors_7d || []).length ? h("div", { class: "card stack" }, h("h2", { text: "What Googlebot asked for and did not get (7 days)" }),
      h("div", { class: "tablewrap" }, h("table", {}, h("thead", {}, h("tr", {}, h("th", { text: "Address" }), h("th", { text: "Kind" }), h("th", { class: "n", text: "Answer" }), h("th", { class: "n", text: "Fetches" }))),
        h("tbody", {}, ...c.errors_7d.map((e) => h("tr", {},
          h("td", { class: "small path" }, h("div", { text: e.path }),
            e.moved_to ? h("div", { class: "tiny" }, "Moved to ", h("a", { href: e.moved_to, target: "_blank", rel: "noopener", text: shortPath(e.moved_to) || e.moved_to }),
              e.moved_noindex ? ", which is noindex on purpose: a 301 only helps once that page is indexable again" : ": a 301 there keeps the old address's history and links") : null),
          h("td", { class: "small muted", text: e.kind || "" }), h("td", { class: "n", text: String(e.status) }), h("td", { class: "n", text: fmt(e.hits) }))))))) : null,
    h("div", { class: "card stack" }, h("h2", { text: `Indexable pages Googlebot has not fetched in 30 days (${fmt(unseen.length)} of ${fmt(urls.length)})` }),
      unseen.length ? h("ul", { class: "small" }, ...unseen.slice(0, 25).map((u) => h("li", {}, h("a", { href: u.url, target: "_blank", rel: "noopener", text: decodeEntities(u.title || "") || shortPath(u.url) }),
        h("span", { class: "muted tiny", text: ` · ${(GOOGLE_STATE[u.bucket] || u.bucket || "").toString().toLowerCase().replace(/_/g, " ")}` }))))
        : h("p", { class: "small muted", text: "Every indexable page was fetched at least once." }),
      unseen.length > 25 ? h("p", { class: "tiny muted", text: `The 25 most valuable shown. Links from pages Googlebot does fetch are the usual way to bring them back into its crawl.` }) : null),
    after);
}

function crawlAfterCard() {
  const a = S.summary && S.summary.crawl_after;
  const k = (a && a.kinds) || {};
  const NAME = { link: "Pages that got a new internal link", cleanup: "Posts the cleanup queue changed", prune: "Pages the prune queue set to noindex or 301" };
  const lines = Object.keys(NAME).filter((x) => k[x] && (k[x].n || k[x].pending)).map((x) => h("div", { class: "kpi" },
    h("div", { class: "v", text: k[x].n ? pct(k[x].refetched / k[x].n) : "–" }),
    h("div", { class: "l", text: `${NAME[x]}: ${fmt(k[x].refetched)} of ${fmt(k[x].n)} fetched again by Googlebot since the change${k[x].pending ? ` (${fmt(k[x].pending)} changed too recently to tell)` : ""}` })));
  if (!lines.length) return null;
  return h("div", { class: "card stack" }, h("h2", { text: "Did Google come back after the engine's changes? (last 30 days)" }), h("div", { class: "grid kpis" }, ...lines),
    h("p", { class: "small muted", text: "The earliest signal there is: Search Console takes weeks to report an indexing change, Cloudflare sees the fetch the same day. A page Googlebot has not fetched since the change cannot have been re-judged yet." }));
}

/* ------------------------------------------------------------------ randomized experiment */
function experimentCard() {
  const E = (S.summary && S.summary.experiment) || {};
  const meta = E._meta || {};
  const NAME = { link: "Internal links", cleanup: "Content cleanups" };
  const kinds = Object.keys(NAME).filter((k) => E[k]);
  const body = kinds.length ? kinds.map((k) => {
    const x = E[k], t = x.treat || {}, hd = x.hold || {};
    const lift = x.lift === null || x.lift === undefined ? "–" : `${x.lift > 0 ? "+" : ""}${Math.round(x.lift * 100)} pts`;
    const verdict = !x.ready ? `Collecting: ${fmt(x.enrolled.treat)} pages offered the change, ${fmt(x.enrolled.hold)} held back. ${x.next ? `First pages measurable from ${x.next}.` : ""} A result counts once each group has ${meta.min_arm || 15} measured pages.`
      : x.p_value !== null && x.p_value < 0.05 ? `${x.lift > 0 ? "The change works" : "The change does not help"}: the difference is unlikely to be luck (p = ${x.p_value}).`
      : `No clear difference yet (p = ${x.p_value}); more pages are added every run.`;
    return h("div", { class: "stack" }, h("h3", { text: NAME[k] }),
      h("div", { class: "grid kpis" },
        h("div", { class: "kpi" }, h("div", { class: "v", text: t.rate === null || t.rate === undefined ? "–" : pct(t.rate) }), h("div", { class: "l", text: `indexed within ${meta.outcome_days || 30} days when offered the change (${fmt(t.n)} measured)` })),
        h("div", { class: "kpi" }, h("div", { class: "v", text: hd.rate === null || hd.rate === undefined ? "–" : pct(hd.rate) }), h("div", { class: "l", text: `indexed when held back (${fmt(hd.n)} measured)` })),
        h("div", { class: "kpi" }, h("div", { class: "v", text: lift }), h("div", { class: "l", text: "difference the change made" }))),
      h("p", { class: "small", text: verdict }));
  }) : [h("p", { class: "small muted", text: "No page enrolled yet: pages join on the next full run." })];
  return h("div", { class: "card stack" }, h("h2", { text: "Controlled experiment" }),
    h("p", { class: "small muted", text: `Of the pages a link or cleanup could be proposed for, a fixed ${meta.pct ?? 10}% is held back for ${meta.hold_days || 31} days, chosen by a hash of the address, so the choice owes nothing to what the engine thinks of the page. ` +
      "Comparing the two groups shows what the change itself does, which the comparison above cannot prove. Once it has enough pages, this result sets how strongly the engine ranks link proposals. The share held back is in Settings (0 switches it off)." }),
    ...body);
}
