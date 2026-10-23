// Run history: the engine opens a run when it starts and closes it with a summary.
import { handle, HttpError, json, now, readJson } from "../../../lib/http.js";

export const onRequestPost = handle(async ({ request, env }) => {
  const b = await readJson(request, 256 * 1024);
  if (b.event === "start") {
    const r = await env.DB.prepare("INSERT INTO runs (started_at, status, trigger, options) VALUES (?, 'running', ?, ?)")
      .bind(now(), String(b.trigger || "manual").slice(0, 40), JSON.stringify(b.options || {})).run();
    return json({ ok: true, id: r.meta.last_row_id });
  }
  if (b.event === "finish") {
    if (!["ok", "failed"].includes(b.status)) throw new HttpError(400, "invalid status");
    await env.DB.prepare("UPDATE runs SET finished_at = ?, status = ?, summary = ?, error = ? WHERE id = ?")
      .bind(now(), b.status, b.summary ? JSON.stringify(b.summary) : null, b.error ? String(b.error).slice(0, 4000) : null, Number(b.id))
      .run();
    // Close runs left "running" by a crash more than 6 hours ago.
    await env.DB.prepare("UPDATE runs SET status = 'failed', error = 'no finish reported' WHERE status = 'running' AND started_at < ?")
      .bind(new Date(Date.now() - 6 * 3600 * 1000).toISOString()).run();
    return json({ ok: true });
  }
  throw new HttpError(400, "unknown event");
});
