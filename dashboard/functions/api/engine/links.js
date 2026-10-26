// The engine replaces the open ("proposed") link proposals of the sites it analysed.
// Approved, applied, rejected and undone ones are never touched here.
import { handle, HttpError, json, now, readJson, SITE_RE } from "../../../lib/http.js";

const s = (v, n) => (v === null || v === undefined ? null : String(v).slice(0, n));

export const onRequestPost = handle(async ({ request, env }) => {
  const { proposals, sites } = await readJson(request, 2 * 1024 * 1024);
  if (!Array.isArray(proposals) || proposals.length > 1000) throw new HttpError(400, "proposals must be an array of at most 1000");
  if (!Array.isArray(sites) || !sites.every((x) => SITE_RE.test(x))) throw new HttpError(400, "invalid sites");
  const t = now();
  const ids = new Set(proposals.map((p) => String(p.id)));
  const stmts = [];
  for (const site of sites) {
    const open = await env.DB.prepare("SELECT id FROM link_proposals WHERE site = ? AND status = 'proposed'").bind(site).all();
    for (const r of open.results) if (!ids.has(r.id)) stmts.push(env.DB.prepare("DELETE FROM link_proposals WHERE id = ?").bind(r.id));
  }
  for (const p of proposals) {
    if (!/^[0-9a-f]{16}$/.test(String(p.id)) || !SITE_RE.test(String(p.site))) continue;
    if (!/^https?:\/\//i.test(String(p.source)) || !/^https?:\/\//i.test(String(p.target))) continue;     // real pages only
    stmts.push(env.DB.prepare(
      `INSERT INTO link_proposals (id, site, source, source_title, target, target_title, anchor, context, score, gain, status, created_at, updated_at)
       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'proposed', ?, ?)
       ON CONFLICT(id) DO UPDATE SET source_title = excluded.source_title, target_title = excluded.target_title,
         anchor = excluded.anchor, context = excluded.context, score = excluded.score, gain = excluded.gain, updated_at = excluded.updated_at
       WHERE link_proposals.status = 'proposed'`
    ).bind(p.id, p.site, s(p.source, 600), s(p.source_title, 300), s(p.target, 600), s(p.target_title, 300), s(p.anchor, 200),
      s(p.context, 600), Number(p.score) || 0, Number(p.gain) || 0, t, t));
  }
  for (let i = 0; i < stmts.length; i += 50) await env.DB.batch(stmts.slice(i, i + 50));
  return json({ ok: true, proposals: proposals.length });
});
