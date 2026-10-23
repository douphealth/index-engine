import { makeSessionCookie, safeEqual } from "../../../lib/auth.js";
import { handle, json, readJson } from "../../../lib/http.js";

const WINDOW_S = 15 * 60;
const MAX_FAILURES = 8;

export const onRequestPost = handle(async ({ request, env }) => {
  const ip = request.headers.get("CF-Connecting-IP") || "unknown";
  const since = Math.floor(Date.now() / 1000) - WINDOW_S;
  const { n } = await env.DB.prepare("SELECT COUNT(*) AS n FROM login_attempts WHERE ip = ? AND ts > ?").bind(ip, since).first();
  if (n >= MAX_FAILURES) return json({ error: "Too many attempts. Try again in 15 minutes." }, 429);

  const { password } = await readJson(request, 4096);
  if (env.DASHBOARD_PASSWORD && (await safeEqual(String(password || ""), env.DASHBOARD_PASSWORD))) {
    await env.DB.prepare("DELETE FROM login_attempts WHERE ip = ?").bind(ip).run();
    return json({ ok: true }, 200, { "Set-Cookie": await makeSessionCookie(env) });
  }
  await env.DB.batch([
    env.DB.prepare("INSERT INTO login_attempts (ip, ts) VALUES (?, ?)").bind(ip, Math.floor(Date.now() / 1000)),
    env.DB.prepare("DELETE FROM login_attempts WHERE ts < ?").bind(since - 86400),
  ]);
  return json({ error: "Wrong password." }, 401);
});
