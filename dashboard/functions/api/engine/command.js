// The engine reports progress on a queued command: running -> done | failed.
import { handle, HttpError, json, now, readJson } from "../../../lib/http.js";

export const onRequestPost = handle(async ({ request, env }) => {
  const { id, status, result } = await readJson(request);
  if (!["running", "done", "failed"].includes(status)) throw new HttpError(400, "invalid status");
  const t = now();
  const sql = status === "running"
    ? "UPDATE commands SET status = 'running', picked_at = ? WHERE id = ? AND status = 'pending'"
    : "UPDATE commands SET status = ?, finished_at = ?, result = ? WHERE id = ? AND status IN ('pending', 'running')";
  const stmt = status === "running"
    ? env.DB.prepare(sql).bind(t, Number(id))
    : env.DB.prepare(sql).bind(status, t, String(result || "").slice(0, 4000), Number(id));
  const r = await stmt.run();
  return json({ ok: Boolean(r.meta.changes) });
});
