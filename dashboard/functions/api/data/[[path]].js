// Crawl results the engine uploaded to KV: /api/data/summary, /api/data/site/<site>, /api/data/log
// Values uploaded gzipped are passed through as-is with Content-Encoding: gzip (the browser decompresses).
import { handle, json, SITE_RE } from "../../../lib/http.js";

function keyFor(parts) {
  if (parts.length === 1 && (parts[0] === "summary" || parts[0] === "log" || parts[0] === "health")) return parts[0];
  if (parts.length === 2 && parts[0] === "site" && SITE_RE.test(parts[1])) return `site:${parts[1]}`;
  return null;
}

export const onRequestGet = handle(async ({ params, env }) => {
  const key = keyFor([].concat(params.path || []));
  if (!key) return json({ error: "not found" }, 404);
  const { value, metadata } = await env.DATA.getWithMetadata(key, { type: "stream" });
  if (value === null) return json({ error: "no data yet" }, 404);
  const headers = {
    "Content-Type": key === "log" ? "text/plain; charset=utf-8" : "application/json; charset=utf-8",
    "Cache-Control": "private, no-store",
  };
  if (metadata && metadata.gzip) {
    headers["Content-Encoding"] = "gzip";
    return new Response(value, { headers, encodeBody: "manual" });
  }
  return new Response(value, { headers });
});
