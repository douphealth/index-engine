// Every request passes through here (public/_routes.json includes "/*").
// Owner pages and /api/* need a session; /api/engine/* needs the engine token; only the login page is open.
import { engineAuthorized, sessionValid } from "../lib/auth.js";
import { json, withSecurityHeaders } from "../lib/http.js";

// Pages serves /login.html at /login (it strips .html), so both spellings are open.
const OPEN = new Set(["/login", "/login.html", "/login.js", "/app.css", "/favicon.svg", "/api/auth/login"]);

export async function onRequest(ctx) {
  const { request, env } = ctx;
  const path = new URL(request.url).pathname;

  if (path.startsWith("/api/engine/")) {
    if (!(await engineAuthorized(request, env))) return withSecurityHeaders(json({ error: "unauthorized" }, 401));
    return withSecurityHeaders(await ctx.next());
  }
  if (OPEN.has(path)) return withSecurityHeaders(await ctx.next());

  if (!(await sessionValid(request, env))) {
    if (path.startsWith("/api/")) return withSecurityHeaders(json({ error: "unauthorized" }, 401));
    return Response.redirect(new URL("/login", request.url).toString(), 302);
  }
  // State-changing owner calls must be same-origin JSON (with the SameSite=Strict cookie this blocks CSRF).
  if (request.method !== "GET" && request.method !== "HEAD") {
    const ct = request.headers.get("Content-Type") || "";
    const origin = request.headers.get("Origin");
    if (!ct.startsWith("application/json") || (origin && origin !== new URL(request.url).origin)) {
      return withSecurityHeaders(json({ error: "forbidden" }, 403));
    }
  }
  return withSecurityHeaders(await ctx.next());
}
