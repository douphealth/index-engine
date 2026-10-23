// Owner decisions about one URL: ignore / unignore / requested (in Search Console) / unrequested / note.
import { handle, HttpError, json, now, readJson, SITE_RE } from "../../lib/http.js";

export const onRequestPost = handle(async ({ request, env }) => {
  const { url, site, action, note } = await readJson(request);
  let u;
  try {
    u = new URL(url);
  } catch {
    throw new HttpError(400, "invalid url");
  }
  if (!/^https?:$/.test(u.protocol) || !SITE_RE.test(site || "") || u.hostname.replace(/^www\./, "") !== site) {
    throw new HttpError(400, "url does not belong to site");
  }
  const t = now();
  const set = {
    ignore: ["ignored = 1", []],
    unignore: ["ignored = 0", []],
    requested: ["requested_at = ?", [t]],
    unrequested: ["requested_at = NULL", []],
    note: ["note = ?", [String(note || "").slice(0, 2000)]],
  }[action];
  if (!set) throw new HttpError(400, "unknown action");
  await env.DB.prepare(
    `INSERT INTO url_state (url, site, updated_at) VALUES (?, ?, ?)
     ON CONFLICT(url) DO NOTHING`
  ).bind(u.toString(), site, t).run();
  await env.DB.prepare(`UPDATE url_state SET ${set[0]}, updated_at = ? WHERE url = ?`).bind(...set[1], t, u.toString()).run();
  const row = await env.DB.prepare("SELECT * FROM url_state WHERE url = ?").bind(u.toString()).first();
  return json({ ok: true, url_state: row });
});
