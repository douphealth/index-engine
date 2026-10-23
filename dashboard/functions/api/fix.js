import { handle, HttpError, json, now, readJson } from "../../lib/http.js";

export const onRequestPost = handle(async ({ request, env }) => {
  const { id, status } = await readJson(request);
  if (!["open", "done", "skipped"].includes(status)) throw new HttpError(400, "invalid status");
  const r = await env.DB.prepare("UPDATE fixes SET status = ?, updated_at = ? WHERE id = ?").bind(status, now(), String(id)).run();
  if (!r.meta.changes) throw new HttpError(404, "no such fix");
  return json({ ok: true });
});
