// The engine on the owner's PC pulls settings, URL decisions, queued commands and link work; doubles as its heartbeat.
import { handle, json, now, sweepStuckCommands } from "../../../lib/http.js";

// Sites whose own rules forbid unapproved changes (gearuptofit AGENTS.md): never auto-approved.
const STRICT_SITES = ["gearuptofit.com"];

export const onRequestGet = handle(async ({ request, env }) => {
  const host = (request.headers.get("X-Engine-Host") || "").slice(0, 80);
  const capabilities = (request.headers.get("X-Engine-Capabilities") || "").split(",").map(x => x.trim());
  const version = /^[0-9a-f]{7,40}$/.test(request.headers.get("X-Engine-Version") || "") ? request.headers.get("X-Engine-Version") : null;
  const [, settings, urls, commands, links, cleanups, prunes] = await env.DB.batch([
    sweepStuckCommands(env),
    env.DB.prepare("SELECT key, value FROM settings"),
    env.DB.prepare("SELECT url, site, ignored, requested_at FROM url_state WHERE ignored = 1 OR requested_at IS NOT NULL"),
    env.DB.prepare("SELECT * FROM commands WHERE status = 'pending' ORDER BY id"),
    env.DB.prepare("SELECT id, site, source, target, anchor, status FROM link_proposals WHERE status IN ('approved', 'undo') ORDER BY updated_at"),
    // Cleanups are applied only after the owner's approval: no autopilot.
    env.DB.prepare("SELECT id, site, url, sections, status FROM cleanup_proposals WHERE status IN ('approved', 'undo') ORDER BY updated_at"),
    // Prunes (noindex / 301): only ever after the owner's approval, never in Autonomous mode.
    env.DB.prepare("SELECT id, site, url, action, target, status FROM prune_proposals WHERE status IN ('approved', 'undo') ORDER BY updated_at"),
    env.DB.prepare("INSERT INTO settings (key, value, updated_at) VALUES ('engine_last_seen', ?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at")
      .bind(JSON.stringify({ at: now(), host, version, capabilities: capabilities.filter(x => ["run", "doctor"].includes(x)) }), now()),
  ]);
  const s = {};
  for (const r of settings.results) s[r.key] = JSON.parse(r.value);
  // Link autopilot is on unless the owner switched it off. The effective value goes back in the settings, so an
  // engine that reads the setting directly (older versions) follows the same rule.
  // Autonomous mode (one switch in Settings) implies the link autopilot and adds the cleanup queue's proposals.
  // Strict sites stay out of both unless the owner included them explicitly.
  s.autonomous = s.autonomous === true;
  s.link_autopilot = s.autonomous || s.link_autopilot !== false;
  const excluded = s.autonomous && s.autonomous_strict_sites === true ? ["-"] : STRICT_SITES;
  const notIn = `site NOT IN (${excluded.map(() => "?").join(",")})`;
  let work = links.results;
  if (s.link_autopilot) {
    const auto = await env.DB.prepare(
      `SELECT id, site, source, target, anchor, status FROM link_proposals
       WHERE status = 'proposed' AND ${notIn} ORDER BY gain DESC LIMIT 60`
    ).bind(...excluded).all();
    work = work.concat(auto.results);
  }
  let cleanupWork = cleanups.results;
  if (s.autonomous) {
    const auto = await env.DB.prepare(
      `SELECT id, site, url, sections, status FROM cleanup_proposals
       WHERE status = 'proposed' AND ${notIn} ORDER BY words DESC LIMIT 120`
    ).bind(...excluded).all();
    cleanupWork = cleanupWork.concat(auto.results);
  }
  return json({
    settings: s,
    url_state: urls.results,
    commands: commands.results.filter(c => c.kind === "run" || (c.kind === "doctor" && capabilities.includes("doctor"))).map((c) => ({ ...c, options: JSON.parse(c.options || "{}") })),
    links: work,
    cleanups: cleanupWork,
    prunes: prunes.results,
  });
});
