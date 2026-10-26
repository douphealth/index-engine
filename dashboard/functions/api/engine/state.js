// The engine on the owner's PC pulls settings, URL decisions, queued commands and link work; doubles as its heartbeat.
import { handle, json, now, sweepStuckCommands } from "../../../lib/http.js";

// Sites whose own rules forbid unapproved changes (gearuptofit AGENTS.md): never auto-approved.
const STRICT_SITES = ["gearuptofit.com"];

export const onRequestGet = handle(async ({ request, env }) => {
  const host = (request.headers.get("X-Engine-Host") || "").slice(0, 80);
  const [, settings, urls, commands, links] = await env.DB.batch([
    sweepStuckCommands(env),
    env.DB.prepare("SELECT key, value FROM settings"),
    env.DB.prepare("SELECT url, site, ignored, requested_at FROM url_state WHERE ignored = 1 OR requested_at IS NOT NULL"),
    env.DB.prepare("SELECT * FROM commands WHERE status = 'pending' ORDER BY id"),
    env.DB.prepare("SELECT id, site, source, target, anchor, status FROM link_proposals WHERE status IN ('approved', 'undo') ORDER BY updated_at"),
    env.DB.prepare("INSERT INTO settings (key, value, updated_at) VALUES ('engine_last_seen', ?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at")
      .bind(JSON.stringify({ at: now(), host }), now()),
  ]);
  const s = {};
  for (const r of settings.results) s[r.key] = JSON.parse(r.value);
  let work = links.results;
  if (s.link_autopilot === true) {
    const auto = await env.DB.prepare(
      `SELECT id, site, source, target, anchor, status FROM link_proposals
       WHERE status = 'proposed' AND site NOT IN (${STRICT_SITES.map(() => "?").join(",")}) ORDER BY gain DESC LIMIT 60`
    ).bind(...STRICT_SITES).all();
    work = work.concat(auto.results);
  }
  return json({
    settings: s,
    url_state: urls.results,
    commands: commands.results.map((c) => ({ ...c, options: JSON.parse(c.options || "{}") })),
    links: work,
  });
});
