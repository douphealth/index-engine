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
  if (body.kind !== "run") throw new HttpError(400, "unknown command");
  await sweepStuckCommands(env).run();
  const pending = await env.DB.prepare("SELECT * FROM commands WHERE status IN ('pending', 'running') ORDER BY id LIMIT 1").first();
  if (pending) return json({ ok: true, command: pending, note: "a run is already queued or running" });
  const options = JSON.stringify(cleanOptions(body.options));
  const r = await env.DB.prepare("INSERT INTO commands (created_at, kind, options) VALUES (?, 'run', ?)").bind(now(), options).run();
  return json({ ok: true, id: r.meta.last_row_id });
});
