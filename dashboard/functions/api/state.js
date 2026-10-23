// Everything the owner manages, in one call: settings, URL decisions, fixes, command queue, run history,
// alerts and the link approval queue.
import { handle, json } from "../../lib/http.js";

export const onRequestGet = handle(async ({ env }) => {
  const [settings, urls, fixes, commands, runs, alerts, links] = await env.DB.batch([
    env.DB.prepare("SELECT key, value, updated_at FROM settings"),
    env.DB.prepare("SELECT url, site, ignored, requested_at, note, updated_at FROM url_state"),
    env.DB.prepare("SELECT * FROM fixes ORDER BY status = 'open' DESC, updated_at DESC"),
    env.DB.prepare("SELECT * FROM commands ORDER BY id DESC LIMIT 25"),
    env.DB.prepare("SELECT id, started_at, finished_at, status, trigger, options, summary, error FROM runs ORDER BY id DESC LIMIT 90"),
    env.DB.prepare("SELECT id, at, site, url, kind, severity, message, acked FROM alerts ORDER BY acked, at DESC LIMIT 300"),
    env.DB.prepare(
      `SELECT * FROM link_proposals ORDER BY CASE status WHEN 'approved' THEN 0 WHEN 'undo' THEN 1 WHEN 'proposed' THEN 2
       WHEN 'applied' THEN 3 WHEN 'failed' THEN 4 ELSE 5 END, gain DESC LIMIT 600`),
  ]);
  const s = {};
  for (const r of settings.results) s[r.key] = { value: JSON.parse(r.value), updated_at: r.updated_at };
  return json({
    settings: s,
    url_state: urls.results,
    fixes: fixes.results,
    commands: commands.results.map((c) => ({ ...c, options: JSON.parse(c.options || "{}") })),
    runs: runs.results.map((r) => ({ ...r, options: JSON.parse(r.options || "{}"), summary: r.summary ? JSON.parse(r.summary) : null })),
    alerts: alerts.results,
    links: links.results,
  });
});
