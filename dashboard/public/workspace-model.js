/* Pure, testable decisions over recorded evidence. No inferred indexing guarantees. */
(function (root) {
  "use strict";
  const DAY = 86400000;
  const list = v => Array.isArray(v) ? v : [];
  const time = v => typeof v === "string" && v ? Date.parse(v) : NaN;
  const num = v => v !== "" && v !== null && v !== undefined && Number.isFinite(Number(v)) ? Number(v) : null;
  const domain = v => {
    try { return new URL(v.includes("://") ? v : "https://" + v).hostname.toLowerCase().replace(/^www\./, ""); }
    catch { return ""; }
  };
  function pageURL(value, site) {
    try {
      const u = new URL(value);
      return /^https?:$/.test(u.protocol) && !u.username && !u.password && domain(u.href) === domain(site) ? u.href : null;
    } catch { return null; }
  }
  function age(value, now = Date.now()) {
    const t = time(value);
    return Number.isFinite(t) && t <= now + 300000 ? Math.max(0, (now - t) / DAY) : null;
  }
  function setting(state, key, fallback) {
    const entry = state?.settings?.[key];
    return entry && Object.hasOwn(entry, "value") ? entry.value : fallback;
  }
  const TYPES = Object.freeze({
    SERVER_ERROR: [0, "technical", "Server error", "Restore a successful response, then re-audit."],
    FETCH_FAILED: [0, "technical", "Fetch failed", "Check origin availability and crawler access; do not assume Google sees the same failure."],
    BROKEN: [1, "recovery", "Broken URL", "Recover a valuable page or choose a relevant redirect after reviewing its history."],
    ROBOTS_BLOCKED: [2, "exclusions", "Robots exclusion", "Confirm whether this exclusion is intentional before changing robots.txt."],
    NOINDEX: [2, "exclusions", "Noindex directive", "Confirm whether the page should be indexed before removing noindex."],
    REDIRECTS: [2, "exclusions", "Redirected URL", "Verify the destination and remove unintended redirecting URLs from sitemaps."],
    CANONICAL_ELSEWHERE: [2, "exclusions", "Canonical elsewhere", "Review canonical ownership; another canonical can be intentional."],
    NOT_HTML: [2, "exclusions", "Non-HTML response", "Check the intended content type; non-HTML is not automatically an error."],
    GOOGLE_PICKED_OTHER: [2, "discovery", "Google chose another canonical", "Compare the canonical pages before requesting another crawl."],
    GOOGLE_STALE: [2, "discovery", "Changed since Google's last look", "Review the live fix and Google's latest inspection separately."],
    NOT_INSPECTED: [2, "evidence", "No Google inspection", "Collect inspection evidence; missing data does not mean not indexed."],
    NOT_CHECKED: [2, "evidence", "Not audited", "Run a measure-only audit before recommending changes."],
    UNKNOWN_TO_GOOGLE: [3, "discovery", "Unknown to Google", "Verify indexability, sitemap inclusion and relevant internal links."],
    DISCOVERED_NOT_INDEXED: [3, "discovery", "Discovered, not indexed", "Check crawl accessibility and internal discovery before repeated requests."],
    CRAWLED_NOT_INDEXED: [3, "content", "Crawled, not indexed", "Review intent, duplication and useful internal links; submitting again is not a fix."],
    INDEXED: [4, "monitor", "Reported indexed", "Monitor the evidence date and changes; a historical report is not a live guarantee."],
  });
  function latest(...dates) {
    const valid = dates.filter(v => Number.isFinite(time(v)));
    return valid.sort((a, b) => time(b) - time(a))[0] || null;
  }
  function verification(row, owner, now) {
    const actionAt = latest(owner?.requested_at, row.linked_at);
    const inspected = time(row.inspected_at);
    const freshness = age(row.inspected_at, now);
    if (!actionAt) return { key: "untracked", label: "No tracked intervention", actionAt: null };
    if (!Number.isFinite(inspected) || inspected <= time(actionAt))
      return { key: "awaiting", label: "Awaiting a later Google inspection", actionAt };
    if (freshness === null || freshness > 14 || row.google_error)
      return { key: "stale", label: "Follow-up evidence needs refreshing", actionAt };
    if (row.google_verdict === "PASS")
      return { key: "observed", label: "Indexed in a later inspection; causation unproven", actionAt };
    return { key: "checked", label: "Inspected after action; not confirmed indexed", actionAt };
  }
  function build(summary, state, tables, now = Date.now()) {
    const owners = new Map(list(state?.url_state).map(x => [x.url, x]));
    const disabled = new Set(list(setting(state, "sites_disabled", [])));
    const items = [], coverage = [], seen = new Set();
    for (const meta of list(summary?.sites)) {
      if (!meta || typeof meta.site !== "string") continue;
      const table = tables instanceof Map ? tables.get(meta.site) : tables?.[meta.site];
      if (!table || !Array.isArray(table.cols) || !Array.isArray(table.rows) || !table.cols.includes("url") || !table.cols.includes("bucket") || table.site !== meta.site) {
        coverage.push({ site: meta.site, loaded: false, count: 0, paused: disabled.has(meta.site) });
        continue;
      }
      let invalid = 0, count = 0;
      for (const data of table.rows) {
        if (!Array.isArray(data)) { invalid++; continue; }
        const row = Object.fromEntries(table.cols.map((c, i) => [c, data[i]]));
        const url = pageURL(row.url, meta.site);
        if (!url) { invalid++; continue; }
        if (seen.has(url)) continue;
        seen.add(url); count++;
        const owner = owners.get(row.url) || owners.get(url) || {};
        const flags = String(row.flags || "").split(",").filter(Boolean);
        const [tier, group, label, next] = TYPES[row.bucket] || [2, "evidence", "Unrecognized state", "Refresh the engine and inspect the recorded evidence."];
        const auditAge = age(row.checked_at, now);
        const snapshotAge = age(table.generated_at, now);
        const followup = verification(row, owner, now);
        items.push({ ...row, url, site: meta.site, title: String(row.title || row.url), flags, owner, meta,
          tier, group, label, next: String(row.action || next), ignored: Object.hasOwn(owner, "ignored") ? owner.ignored === 1 || owner.ignored === true : flags.includes("IGNORED"),
          paused: disabled.has(meta.site), auditAge, snapshotAge, followup,
          stale: snapshotAge === null || snapshotAge > 1.5 || auditAge === null || auditAge > 7,
          clicks: (num(row.gsc_clicks) || 0) + (num(row.bing_clicks) || 0),
          clicksKnown: num(row.gsc_clicks) !== null || num(row.bing_clicks) !== null,
          review: row.bucket !== "INDEXED" || flags.some(f => ["TRUST_RISK", "NEAR_DUPLICATE", "HEAVY_PAGE", "NOT_IN_SITEMAP"].includes(f)),
        });
      }
      coverage.push({ site: meta.site, loaded: true, count, invalid, paused: disabled.has(meta.site), generatedAt: table.generated_at });
    }
    items.sort((a, b) => a.tier - b.tier || b.clicks - a.clicks || (num(b.value) || 0) - (num(a.value) || 0) || a.url.localeCompare(b.url));
    return { items, coverage };
  }
  function select(items, options = {}) {
    const q = (options.q || "").trim().toLowerCase();
    return items.filter(r => (!options.site || r.site === options.site)
      && (!options.group || r.group === options.group)
      && (options.includePaused || !r.paused) && (options.includeIgnored || !r.ignored)
      && (options.mode === "all" || (options.mode === "followup" ? r.followup.key !== "untracked" : r.review))
      && (!q || (r.url + " " + r.title + " " + r.label + " " + r.flags.join(" ")).toLowerCase().includes(q)));
  }
  function csv(rows) {
    const cell = value => {
      let s = String(value ?? "");
      if (/^[\s]*[=+@-]/.test(s) || /^[\t\r\n]/.test(s)) s = "'" + s;
      return '"' + s.replace(/"/g, '""') + '"';
    };
    const fields = ["site", "url", "title", "bucket", "label", "checked_at", "inspected_at", "google_verdict", "reason", "next"];
    return [fields, ...rows.map(r => fields.map(k => r[k]))].map(row => row.map(cell).join(",")).join("\r\n");
  }
  root.IndexWorkspace = Object.freeze({ build, select, verification, pageURL, age, num, setting, csv, TYPES });
})(globalThis);
