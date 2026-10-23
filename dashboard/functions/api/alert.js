// Acknowledge one alert, or all open ones.
import { handle, json, readJson } from "../../lib/http.js";

export const onRequestPost = handle(async ({ request, env }) => {
  const { id, all } = await readJson(request);
  const r = all
    ? await env.DB.prepare("UPDATE alerts SET acked = 1 WHERE acked = 0").run()
    : await env.DB.prepare("UPDATE alerts SET acked = 1 WHERE id = ?").bind(Number(id)).run();
  return json({ ok: true, changed: r.meta.changes });
});
