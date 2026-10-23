// Owner session (password -> signed cookie) and engine bearer token.
// Secrets (Pages project secrets): DASHBOARD_PASSWORD, SESSION_SECRET, ENGINE_TOKEN.

const enc = new TextEncoder();
export const COOKIE = "ie_session";
const SESSION_DAYS = 30;

async function hmac(secret, message) {
  const key = await crypto.subtle.importKey("raw", enc.encode(secret), { name: "HMAC", hash: "SHA-256" }, false, ["sign"]);
  const sig = await crypto.subtle.sign("HMAC", key, enc.encode(message));
  return btoa(String.fromCharCode(...new Uint8Array(sig))).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

// Constant-time equality: compare HMACs of both values under a per-call random key.
export async function safeEqual(a, b) {
  if (typeof a !== "string" || typeof b !== "string") return false;
  const k = crypto.randomUUID();
  const [x, y] = await Promise.all([hmac(k, a), hmac(k, b)]);
  let diff = x.length ^ y.length;
  for (let i = 0; i < Math.min(x.length, y.length); i++) diff |= x.charCodeAt(i) ^ y.charCodeAt(i);
  return diff === 0;
}

export async function makeSessionCookie(env) {
  const exp = Math.floor(Date.now() / 1000) + SESSION_DAYS * 86400;
  const value = `${exp}.${await hmac(env.SESSION_SECRET, `session:${exp}`)}`;
  return `${COOKIE}=${value}; Path=/; HttpOnly; Secure; SameSite=Strict; Max-Age=${SESSION_DAYS * 86400}`;
}

export const clearSessionCookie = () => `${COOKIE}=; Path=/; HttpOnly; Secure; SameSite=Strict; Max-Age=0`;

export async function sessionValid(request, env) {
  if (!env.SESSION_SECRET) return false;
  const cookie = request.headers.get("Cookie") || "";
  const m = cookie.match(new RegExp(`(?:^|;\\s*)${COOKIE}=([^;]+)`));
  if (!m) return false;
  const [exp, sig] = m[1].split(".");
  if (!exp || !sig || Number(exp) < Date.now() / 1000) return false;
  return safeEqual(sig, await hmac(env.SESSION_SECRET, `session:${exp}`));
}

export async function engineAuthorized(request, env) {
  const h = request.headers.get("Authorization") || "";
  return Boolean(env.ENGINE_TOKEN) && h.startsWith("Bearer ") && safeEqual(h.slice(7), env.ENGINE_TOKEN);
}
