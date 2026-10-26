// The engine pushes new alerts (deduplicated by engine id + time).
import { handle, HttpError, json, readJson } from "../../../lib/http.js";

const SEVERITY = new Set(["high", "warn", "good", "info"]);
const httpUrl = (u) => (typeof u === "string" && /^https?:\/\//i.test(u) ? u.slice(0, 600) : null);   // only real page URLs are stored

export const onRequestPost = handle(async ({ request, env }) => {
  const { alerts } = await readJson(request, 512 * 1024);
  if (!Array.isArray(alerts) || alerts.length > 500) throw new HttpError(400, "alerts must be an array of at most 500");
  const stmts = alerts.map((a) =>
    env.DB.prepare(
      "INSERT OR IGNORE INTO alerts (engine_key, at, site, url, kind, severity, message) VALUES (?, ?, ?, ?, ?, ?, ?)"
    ).bind(`${Number(a.engine_id)}@${String(a.at).slice(0, 40)}`, String(a.at).slice(0, 40), a.site ? String(a.site).slice(0, 80) : null,
      httpUrl(a.url), String(a.kind).slice(0, 40),
      SEVERITY.has(a.severity) ? a.severity : "info", String(a.message).slice(0, 1000))
  );
  for (let i = 0; i < stmts.length; i += 50) await env.DB.batch(stmts.slice(i, i + 50));
  return json({ ok: true, received: stmts.length });
});
