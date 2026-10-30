// One active command across devices. The INSERT, not a prior SELECT, owns deduplication.
import { handle, HttpError, json, now, readJson, SITE_RE, sweepStuckCommands } from "../../lib/http.js";

function cleanOptions(o = {}) {
  if (!o || typeof o !== "object" || Array.isArray(o)) throw new HttpError(400, "invalid options");
  const out = {};
  if (o.site) {
    if (typeof o.site !== "string" || !SITE_RE.test(o.site)) throw new HttpError(400, "invalid site");
    out.site = o.site;
  }
  for (const k of ["no_submit", "force_audit", "dry_run"]) {
    if (o[k] !== undefined && typeof o[k] !== "boolean") throw new HttpError(400, `invalid ${k}`);
    if (o[k] === true) out[k] = true;
  }
  return out;
}

export const onRequestPost = handle(async ({ request, env }) => {
  const body = await readJson(request);
  if (!body || typeof body !== "object" || Array.isArray(body)) throw new HttpError(400, "expected an object");
  if (body.cancel !== undefined) {
    const id = Number(body.cancel);
    if (!Number.isSafeInteger(id) || id <= 0) throw new HttpError(400, "invalid command id");
    const r = await env.DB.prepare("UPDATE commands SET status = 'cancelled', finished_at = ? WHERE id = ? AND status = 'pending'")
      .bind(now(), id).run();
    return json({ ok: Boolean(r.meta.changes) });
  }
  if (!["run", "doctor"].includes(body.kind)) throw new HttpError(400, "unknown command");
  const options = body.kind === "doctor" ? {} : cleanOptions(body.options);
  if (options.site) {
    const record = await env.DB.prepare("SELECT value FROM settings WHERE key = 'sites_disabled'").first();
    let disabled = [];
    try { disabled = JSON.parse(record?.value || "[]"); } catch { throw new HttpError(503, "Cannot verify site settings"); }
    if (!Array.isArray(disabled)) throw new HttpError(503, "Cannot verify site settings");
    if (disabled.includes(options.site)) throw new HttpError(409, "This website is paused in Settings");
  }
  if (body.kind === "doctor") {
    const latest = await env.DB.prepare("SELECT value FROM settings WHERE key = 'engine_last_seen'").first();
    let seen = {};
    try { seen = JSON.parse(latest?.value || "{}"); } catch { /* fail closed below */ }
    const capabilities = Array.isArray(seen?.capabilities) ? seen.capabilities : [];
    const age = seen?.at ? Date.now() - Date.parse(seen.at) : Infinity;
    if (!capabilities.includes("doctor") || !Number.isFinite(age) || age < 0 || age > 25 * 60 * 1000)
      throw new HttpError(409, "Compatible local engine has not checked in recently; update and start the engine first");
  }
  await sweepStuckCommands(env).run();
  // SQLite evaluates this guard and insert in the same write statement, including concurrent requests.
  const r = await env.DB.prepare(
    "INSERT INTO commands (created_at, kind, options) SELECT ?, ?, ? WHERE NOT EXISTS " +
    "(SELECT 1 FROM commands WHERE status IN ('pending', 'running'))"
  ).bind(now(), body.kind, JSON.stringify(options)).run();
  if (r.meta.changes) return json({ ok: true, id: r.meta.last_row_id, kind: body.kind, status: "pending" });
  const pending = await env.DB.prepare("SELECT * FROM commands WHERE status IN ('pending', 'running') ORDER BY id LIMIT 1").first();
  if (pending) return json({ ok: true, command: pending, note: "A " + pending.kind + " command is already queued or running; no duplicate was created" });
  throw new HttpError(409, "Queue changed during the request; refresh before retrying");
});
