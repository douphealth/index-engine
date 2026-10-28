/* Portfolio Action Center: source-backed derived state only. No network calls, no persistence, no guessed performance. */
(function (root) {
  "use strict";
  const BLOCKERS = ["FETCH_FAILED", "BROKEN", "SERVER_ERROR", "REDIRECTS", "NOINDEX",
    "ROBOTS_BLOCKED", "CANONICAL_ELSEWHERE", "NOT_HTML"];
  const KNOWN = ["INDEXED", "CRAWLED_NOT_INDEXED", "DISCOVERED_NOT_INDEXED",
    "UNKNOWN_TO_GOOGLE", "GOOGLE_STALE", "GOOGLE_PICKED_OTHER"];
  const array = (v) => Array.isArray(v) ? v : [];
  const value = (state, name, fallback) => {
    const entry = (state.settings || {})[name];
    return entry && Object.prototype.hasOwnProperty.call(entry, "value") ? entry.value : fallback;
  };
  const ageMinutes = (date, now) => {
    const time = typeof date === "string" ? Date.parse(date) : NaN;
    return Number.isFinite(time) ? Math.max(0, (now - time) / 60000) : null;
  };
  const number = (n) => Number.isFinite(Number(n)) ? Math.max(0, Number(n)) : 0;

  function derive(summary, state, opts = {}) {
    summary = summary || {};
    state = state || {};
    const now = Number.isFinite(opts.now) ? opts.now : Date.now();
    const focus = typeof opts.focus === "string" ? opts.focus : "";
    const disabled = new Set(array(value(state, "sites_disabled", [])));
    const sites = array(summary.sites).filter((s) => s && s.site && (!focus || s.site === focus)).map((s) => {
      const counts = s.counts || {};
      const count = (key) => number(counts[key]);
      const known = KNOWN.reduce((acc, k) => acc + count(k), 0);
      return {
        site: s.site, enabled: !disabled.has(s.site), urls: number(s.urls),
        indexed: count("INDEXED"), crawled: count("CRAWLED_NOT_INDEXED"),
        discovered: count("DISCOVERED_NOT_INDEXED"), unknown: count("UNKNOWN_TO_GOOGLE"),
        blockers: BLOCKERS.reduce((acc, k) => acc + count(k), 0),
        known, share: known >= 10 ? count("INDEXED") / known : null,
        gsc: Boolean(s.gsc_property), bing: Boolean(s.bing_site),
        conflicts: number(s.conflicts), flags: s.flags || {},
        traffic: s.traffic || null,
      };
    });
    const enabled = sites.filter((s) => s.enabled);
    const sum = (key) => enabled.reduce((n, s) => n + number(s[key]), 0);
    const engine = value(state, "engine_last_seen", null);
    const lastSeen = engine && engine.at;
    const heartbeatAgeMinutes = ageMinutes(lastSeen, now);
    const dataAgeMinutes = ageMinutes(summary.generated_at, now);
    const paused = value(state, "submit_paused", false) === true;
    const connected = heartbeatAgeMinutes !== null && heartbeatAgeMinutes < 25;
    const fresh = dataAgeMinutes !== null && dataAgeMinutes <= 36 * 60;
    const urls = new Map(array(state.url_state).map((u) => [u.url, u]));
    const requests = array(summary.requests).filter((r) => {
      if (focus && r.site !== focus) return false;
      if (disabled.has(r.site)) return false;
      const u = urls.get(r.url);
      if (u && u.ignored) return false;
      if (u && u.requested_at && ageMinutes(u.requested_at, now) < 14 * 1440) return false;
      return true;
    });
    const links = array(state.links).filter((l) => (!focus || l.site === focus) && !disabled.has(l.site));
    const actions = array(summary.actions).filter((a) => (!focus || a.site === focus) && !disabled.has(a.site));
    const alerts = array(state.alerts).filter((a) => !a.acked && (!focus || a.site === focus || !a.site)
      && (!a.site || !disabled.has(a.site)));
    const highAlerts = alerts.filter((a) => a.severity === "high");
    const runs = array(state.runs);
    const lastRun = runs[0] || null;
    const lastSuccess = runs.find((r) => r.status === "ok") || null;
    const commands = array(state.commands);
    const queued = commands.find((c) => c.status === "pending" || c.status === "running") || null;
    const missingGsc = enabled.filter((s) => !s.gsc);
    const highRisk = [...enabled].sort((a, b) => (b.blockers + b.conflicts + b.crawled)
      - (a.blockers + a.conflicts + a.crawled));
    const blockedSite = enabled.find((s) => s.blockers > 0);
    const issueSite = highRisk[0];
    const activeSites = enabled.length;
    const problems = sum("blockers");
    const toImprove = sum("crawled") + sum("discovered") + sum("unknown");
    const proposed = links.filter((l) => l.status === "proposed").length;
    const impact = summary.impact || {};
    // A clear drop is one the engine judged both large and statistically clear; small numbers are never called.
    const drops = [];
    for (const s of enabled) {
      for (const src of ["bing", "google"]) {
        const t = s.traffic && s.traffic[src];
        if (t && t.clicks && t.clicks.verdict === "declining") {
          drops.push({ site: s.site, src, pct: t.clicks.pct, prev: number(t.clicks_prev), now: number(t.clicks_now), confidence: t.clicks.confidence });
        }
      }
    }
    drops.sort((a, b) => (b.prev - b.now) - (a.prev - a.now));
    const hasTraffic = enabled.some((s) => s.traffic && (s.traffic.bing || s.traffic.google));
    const worst = drops[0];
    const steps = [
      {
        number: 1, title: "Check connections & freshness",
        metric: connected && fresh ? "Engine reporting" : "Check data health",
        status: !connected || !fresh || missingGsc.length || (lastRun && lastRun.status === "failed") ? "attention" : "clear",
        detail: !connected ? "Local engine has not checked in recently. Scheduled checks depend on your PC."
          : !fresh ? "The latest uploaded results are older than 36 hours or unavailable."
          : missingGsc.length ? missingGsc.length + " enabled site(s) lack a verified GSC property in the last report."
          : "Recent engine heartbeat and results available. Review any failed runs.",
        href: "#/runs", cta: "Review run health",
      },
      {
        number: 2, title: "Review technical blockers",
        metric: problems.toLocaleString() + " excluded / failed URLs",
        status: problems || highAlerts.length ? "attention" : "clear",
        detail: "Review 4xx/5xx, noindex, redirects, robots and canonicals. Some exclusions are intentional; do not bulk-remove them.",
        href: blockedSite ? "#/site/" + encodeURIComponent(blockedSite.site) : "#/fixes",
        cta: problems ? "Inspect affected URLs" : "Review fixes",
      },
      {
        number: 3, title: "Recover lost search traffic",
        metric: !hasTraffic ? "No traffic history yet" : drops.length ? drops.length + " clear traffic drop" + (drops.length > 1 ? "s" : "") : "No clear traffic drop",
        status: drops.length ? "attention" : hasTraffic ? "clear" : "ready",
        detail: worst ? worst.site + ": " + (worst.src === "bing" ? "Bing" : "Google") + " clicks fell " + Math.abs(Math.round(worst.pct)) + "% ("
            + worst.prev.toLocaleString() + " to " + worst.now.toLocaleString() + ") in the latest 28 days. Open Revival to see which pages lost clicks and what holds each back."
          : hasTraffic ? "Compared with the previous 28 days, no site shows a drop that is both large and statistically clear. Small numbers are never called."
          : "The next engine run records daily clicks from Bing and Google.",
        href: worst ? "#/revival?site=" + encodeURIComponent(worst.site) : "#/revival", cta: worst ? "See what lost traffic" : "Open Revival",
      },
      {
        number: 4, title: "Strengthen valuable pages",
        metric: toImprove.toLocaleString() + " not indexed yet",
        status: toImprove || requests.length || proposed || actions.length ? "ready" : "clear",
        detail: proposed ? proposed + " contextual link proposal(s) ready for review. Prioritize pages with search demand."
          : "Use ranked page recommendations, internal links and relevant content repairs; avoid duplicate submissions.",
        href: proposed ? "#/links" : "#/plan", cta: proposed ? "Review links" : "Open prioritized plan",
      },
      {
        number: 5, title: "Verify observed outcomes",
        metric: lastSuccess ? "Last successful run recorded" : "No completed run recorded",
        status: lastSuccess && fresh ? "ready" : "attention",
        detail: "Check Google's latest recorded verdicts and measured gains/losses. Submission is not proof of indexing.",
        href: "#/overview", cta: "See evidence & trends",
      },
    ];
    return {
      sites, enabled, activeSites, totalSites: sites.length, paused, connected, fresh,
      lastSeen, heartbeatAgeMinutes, dataAgeMinutes, lastRun, lastSuccess, queued, missingGsc,
      problems, toImprove, requests: requests.length, proposed, actions: actions.length,
      highAlerts: highAlerts.length, alerts: alerts.length, indexed: sum("indexed"),
      known: sum("known"), urls: sum("urls"), impact, steps, issueSite, drops, hasTraffic,
    };
  }

  const publicApi = Object.freeze({ derive });
  root.IndexGuide = publicApi;
  if (typeof module !== "undefined" && module.exports) module.exports = publicApi;
})(typeof globalThis === "object" ? globalThis : this);
