// The engine replaces the open ("proposed") prune proposals of the sites it analysed.
// Approved, applied, rejected and undone ones are never touched here.
import { handle, HttpError, json, now, readJson, SITE_RE } from "../../../lib/http.js";

const s = (v, n) => (v === null || v === undefined ? null : String(v).slice(0, n));
const ACTIONS = new Set(["noindex", "redirect"]);
const EVIDENCE = ["bucket", "flags", "google_impr_90d", "bing_impr", "words", "age_days", "inbound_links"];

export const onRequestPost = handle(async ({ request, env }) => {
  const { proposals, sites } = await readJson(request, 4 * 1024 * 1024);
  if (!Array.isArray(proposals) || proposals.length > 3000) throw new HttpError(400, "proposals must be an array of at most 3000");
  if (!Array.isArray(sites) || !sites.every((x) => SITE_RE.test(x))) throw new HttpError(400, "invalid sites");
  const t = now();
  const ids = new Set(proposals.map((p) => String(p.id)));
  const stmts = [];
  for (const site of sites) {
    const open = await env.DB.prepare("SELECT id FROM prune_proposals WHERE site = ? AND status = 'proposed'").bind(site).all();
    for (const r of open.results) if (!ids.has(r.id)) stmts.push(env.DB.prepare("DELETE FROM prune_proposals WHERE id = ?").bind(r.id));
  }
  for (const p of proposals) {
    if (!/^[0-9a-f]{16}$/.test(String(p.id)) || !SITE_RE.test(String(p.site)) || !sites.includes(p.site)) continue;
    if (!/^https?:\/\//i.test(String(p.url)) || !ACTIONS.has(p.action)) continue;
    if (p.action === "redirect" && !/^https?:\/\//i.test(String(p.target || ""))) continue;
    const ev = {};
    for (const k of EVIDENCE) if (p.evidence && p.evidence[k] !== undefined) ev[k] = typeof p.evidence[k] === "string" ? s(p.evidence[k], 200) : p.evidence[k];
    stmts.push(env.DB.prepare(
      `INSERT INTO prune_proposals (id, site, url, title, action, target, why, evidence, status, created_at, updated_at)
       VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'proposed', ?, ?)
       ON CONFLICT(id) DO UPDATE SET title = excluded.title, action = excluded.action, target = excluded.target,
         why = excluded.why, evidence = excluded.evidence, updated_at = excluded.updated_at
       WHERE prune_proposals.status = 'proposed'`
    ).bind(p.id, p.site, s(p.url, 600), s(p.title, 300), p.action, p.action === "redirect" ? s(p.target, 600) : null,
      s(p.why, 600), JSON.stringify(ev), t, t));
  }
  for (let i = 0; i < stmts.length; i += 50) await env.DB.batch(stmts.slice(i, i + 50));
  return json({ ok: true, proposals: proposals.length });
});
