// Crawl results the engine uploaded to KV: /api/data/summary, /api/data/site/<site>, /api/data/log
import { handle, json, SITE_RE } from "../../../lib/http.js";

function keyFor(parts) {
  if (parts.length === 1 && (parts[0] === "summary" || parts[0] === "log")) return parts[0];
  if (parts.length === 2 && parts[0] === "site" && SITE_RE.test(parts[1])) return `site:${parts[1]}`;
  return null;
}

export const onRequestGet = handle(async ({ params, env }) => {
  const key = keyFor([].concat(params.path || []));
  if (!key) return json({ error: "not found" }, 404);
  const value = await env.DATA.get(key, { type: "stream" });
  if (value === null) return json({ error: "no data yet" }, 404);
  return new Response(value, {
    headers: {
      "Content-Type": key === "log" ? "text/plain; charset=utf-8" : "application/json; charset=utf-8",
      "Cache-Control": "private, no-store",
    },
  });
});
