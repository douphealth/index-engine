// The engine reports what happened to one link: applied | failed | undone.
import { handle, HttpError, json, now, readJson } from "../../../lib/http.js";

export const onRequestPost = handle(async ({ request, env }) => {
  const { id, status, result, applied_at } = await readJson(request);
  if (!["applied", "failed", "undone"].includes(status)) throw new HttpError(400, "invalid status");
  const r = await env.DB.prepare(
    "UPDATE link_proposals SET status = ?, result = ?, applied_at = COALESCE(?, applied_at), updated_at = ? WHERE id = ?"
  ).bind(status, String(result || "").slice(0, 1000), applied_at || null, now(), String(id)).run();
  return json({ ok: Boolean(r.meta.changes) });
});
