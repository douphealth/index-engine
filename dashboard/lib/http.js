// Small helpers shared by every Pages Function.

export const SECURITY_HEADERS = {
  "X-Content-Type-Options": "nosniff",
  "X-Frame-Options": "DENY",
  "Referrer-Policy": "no-referrer",
  "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
  "Content-Security-Policy":
    "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; " +
    "frame-ancestors 'none'; base-uri 'none'; form-action 'self'",
};

export function json(data, status = 200, headers = {}) {
  return new Response(JSON.stringify(data), {
    status,
    headers: { "Content-Type": "application/json; charset=utf-8", "Cache-Control": "no-store", ...headers },
  });
}

export function withSecurityHeaders(response) {
  const r = new Response(response.body, response);
  for (const [k, v] of Object.entries(SECURITY_HEADERS)) r.headers.set(k, v);
  return r;
}

// Parse a JSON body, refusing anything larger than `limit` bytes.
export async function readJson(request, limit = 64 * 1024) {
  const len = Number(request.headers.get("Content-Length") || 0);
  if (len > limit) throw new HttpError(413, "body too large");
  const text = await request.text();
  if (text.length > limit) throw new HttpError(413, "body too large");
  try {
    return JSON.parse(text || "{}");
  } catch {
    throw new HttpError(400, "invalid JSON");
  }
}

export class HttpError extends Error {
  constructor(status, message) {
    super(message);
    this.status = status;
  }
}

export function handle(fn) {
  return async (ctx) => {
    try {
      return await fn(ctx);
    } catch (e) {
      if (e instanceof HttpError) return json({ error: e.message }, e.status);
      console.error(e);
      return json({ error: "internal error" }, 500);
    }
  };
}

export const now = () => new Date().toISOString();

// A run the engine claimed but never reported on (PC switched off, process killed) must not block the queue for ever:
// after this long it is closed as failed so the owner can queue another.
export const STUCK_RUN_HOURS = 3;
export function sweepStuckCommands(env) {
  const cutoff = new Date(Date.now() - STUCK_RUN_HOURS * 3600 * 1000).toISOString();
  return env.DB.prepare(
    "UPDATE commands SET status = 'failed', finished_at = ?, result = ? WHERE status = 'running' AND picked_at < ?"
  ).bind(now(), `The engine stopped before finishing (no report for ${STUCK_RUN_HOURS} hours).`, cutoff);
}

export const SITE_RE = /^[a-z0-9.-]{3,80}$/;
