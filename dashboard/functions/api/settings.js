// Engine settings the owner can change. The engine reads them at the start of every run.
import { handle, HttpError, json, now, readJson, SITE_RE } from "../../lib/http.js";

const VALIDATE = {
  sites_disabled: (v) => Array.isArray(v) && v.length <= 50 && v.every((s) => SITE_RE.test(s)),
  submit_paused: (v) => typeof v === "boolean",
  manual_queue_per_site: (v) => Number.isInteger(v) && v >= 0 && v <= 50,
  inspect_max_per_site: (v) => v === null || (Number.isInteger(v) && v >= 0 && v <= 2000),
  daily_run_time: (v) => typeof v === "string" && /^([01]\d|2[0-3]):[0-5]\d$/.test(v),
};

export const onRequestPost = handle(async ({ request, env }) => {
  const body = await readJson(request);
  const t = now();
  const stmts = [];
  for (const [k, v] of Object.entries(body)) {
    if (!VALIDATE[k]) throw new HttpError(400, `unknown setting ${k}`);
    if (!VALIDATE[k](v)) throw new HttpError(400, `invalid value for ${k}`);
    stmts.push(
      env.DB.prepare("INSERT INTO settings (key, value, updated_at) VALUES (?, ?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at")
        .bind(k, JSON.stringify(v), t)
    );
  }
  if (stmts.length) await env.DB.batch(stmts);
  return json({ ok: true, saved: stmts.length });
});
