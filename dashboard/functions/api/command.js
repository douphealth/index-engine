// Queue a run for the engine (or cancel a pending one). One pending run at a time.
import { handle, HttpError, json, now, readJson, SITE_RE, sweepStuckCommands } from "../../lib/http.js";

function cleanOptions(o = {}) {
  const out = {};
  if (o.site) {
    if (!SITE_RE.test(o.site)) throw new HttpError(400, "invalid site");
    out.site = o.site;
  }
  for (const k of ["no_submit", "force_audit", "dry_run"]) if (o[k] === true) out[k] = true;
  return out;
}

export const onRequestPost = handle(async ({ request, env }) => {
  const body = await readJson(request);
  if (body.cancel) {
    const r = await env.DB.prepare("UPDATE commands SET status = 'cancelled', finished_at = ? WHERE id = ? AND status = 'pending'")
      .bind(now(), Number(body.cancel)).run();
    return json({ ok: Boolean(r.meta.changes) });
  }
  if (!["run", "doctor"].includes(body.kind)) throw new HttpError(400, "unknown command");
  if (body.kind === "doctor") {
    const latest = await env.DB.prepare("SELECT value FROM settings WHERE key = 'engine_last_seen'").first();
    let capabilities = [];
    try { capabilities = JSON.parse(latest?.value || "{}").capabilities || []; } catch { /* unavailable */ }
    let heartbeatAt = null;
    try { heartbeatAt = JSON.parse(latest?.value || "{}").at || null; } catch { /* unavailable */ }
    const age = heartbeatAt ? Date.now() - Date.parse(heartbeatAt) : Infinity;
    if (!capabilities.includes("doctor") || !Number.isFinite(age) || age < 0 || age > 25 * 60 * 1000)
      throw new HttpError(409, "Compatible local engine has not checked in recently; update and start the engine first");
  }
  await sweepStuckCommands(env).run();
  const pending = await env.DB.prepare("SELECT * FROM commands WHERE status IN ('pending', 'running') ORDER BY id LIMIT 1").first();
  if (pending) return json({ ok: true, command: pending, note: "a run is already queued or running" });
  const options = JSON.stringify(body.kind === "doctor" ? {} : cleanOptions(body.options));
  const r = await env.DB.prepare("INSERT INTO commands (created_at, kind, options) VALUES (?, ?, ?)").bind(now(), body.kind, options).run();
  return json({ ok: true, id: r.meta.last_row_id });
});
