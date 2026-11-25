// Crawl results the engine uploaded to KV: /api/data/summary, /api/data/site/<site>, /api/data/log
// Values uploaded gzipped are passed through as-is with Content-Encoding: gzip (the browser decompresses).
//
// Each value carries an ETag made from the time the engine uploaded it. The browser keeps its copy (private: never a
// shared cache) but must ask before every use (no-cache); when nothing was uploaded since, the answer is an empty 304
// and the page is drawn from the copy it already has, instead of downloading a site's whole URL table again.
import { handle, json, SITE_RE } from "../../../lib/http.js";

function keyFor(parts) {
  if (parts.length === 1 && (parts[0] === "summary" || parts[0] === "log" || parts[0] === "health")) return parts[0];
  if (parts.length === 2 && parts[0] === "site" && SITE_RE.test(parts[1])) return `site:${parts[1]}`;
  return null;
}

// "2026-11-26T09:15:02.123Z" -> "\"20261126091502123\""; null when the value was stored before uploads were dated.
export function etagFor(metadata) {
  const digits = String((metadata && metadata.updated_at) || "").replace(/\D/g, "");
  return digits.length >= 14 ? `"${digits}${metadata.gzip ? "g" : ""}"` : null;
}

function matches(header, etag) {
  if (!header || !etag) return false;
  return header.split(",").some((t) => t.trim().replace(/^W\//, "") === etag);
}

export const onRequestGet = handle(async ({ params, env, request }) => {
  const key = keyFor([].concat(params.path || []));
  if (!key) return json({ error: "not found" }, 404);
  const { value, metadata } = await env.DATA.getWithMetadata(key, { type: "stream" });
  if (value === null) return json({ error: "no data yet" }, 404);
  const etag = etagFor(metadata);
  const headers = {
    "Content-Type": key === "log" ? "text/plain; charset=utf-8" : "application/json; charset=utf-8",
    "Cache-Control": etag ? "private, no-cache" : "private, no-store",
  };
  if (etag) headers.ETag = etag;
  if (matches(request && request.headers.get("If-None-Match"), etag)) {
    if (value.cancel) await value.cancel();
    return new Response(null, { status: 304, headers: { "Cache-Control": headers["Cache-Control"], ETag: etag } });
  }
  if (metadata && metadata.gzip) {
    headers["Content-Encoding"] = "gzip";
    return new Response(value, { headers, encodeBody: "manual" });
  }
  return new Response(value, { headers });
});
