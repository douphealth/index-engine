// The engine uploads its results: summary, log, site:<domain>. Streamed straight into KV (no parsing).
import { handle, json, SITE_RE } from "../../../../lib/http.js";

const MAX_BYTES = 20 * 1024 * 1024;

export const onRequestPut = handle(async ({ request, params, env }) => {
  const key = String(params.key || "");
  const ok = key === "summary" || key === "log" || (key.startsWith("site:") && SITE_RE.test(key.slice(5)));
  if (!ok) return json({ error: "invalid key" }, 400);
  if (Number(request.headers.get("Content-Length") || 0) > MAX_BYTES) return json({ error: "too large" }, 413);
  await env.DATA.put(key, request.body, { metadata: { updated_at: new Date().toISOString() } });
  return json({ ok: true, key });
});
