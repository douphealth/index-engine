// The engine on the owner's PC pulls settings, URL decisions and queued commands; doubles as its heartbeat.
import { handle, json, now } from "../../../lib/http.js";

export const onRequestGet = handle(async ({ request, env }) => {
  const host = (request.headers.get("X-Engine-Host") || "").slice(0, 80);
  const [settings, urls, commands] = await env.DB.batch([
    env.DB.prepare("SELECT key, value FROM settings"),
    env.DB.prepare("SELECT url, site, ignored, requested_at FROM url_state WHERE ignored = 1 OR requested_at IS NOT NULL"),
    env.DB.prepare("SELECT * FROM commands WHERE status = 'pending' ORDER BY id"),
    env.DB.prepare("INSERT INTO settings (key, value, updated_at) VALUES ('engine_last_seen', ?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at")
      .bind(JSON.stringify({ at: now(), host }), now()),
  ]);
  const s = {};
  for (const r of settings.results) s[r.key] = JSON.parse(r.value);
  return json({
    settings: s,
    url_state: urls.results,
    commands: commands.results.map((c) => ({ ...c, options: JSON.parse(c.options || "{}") })),
  });
});
