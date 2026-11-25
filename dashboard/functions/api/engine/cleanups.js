// The engine replaces the open ("proposed") cleanup proposals of the sites it analysed.
// Approved, applied, rejected and undone ones are never touched here.
import { handle, HttpError, json, now, readJson, SITE_RE } from "../../../lib/http.js";

const s = (v, n) => (v === null || v === undefined ? null : String(v).slice(0, n));
const KINDS = new Set(["numbered", "leftover", "boilerplate"]);

function cleanSections(list) {
  if (!Array.isArray(list)) return null;
  const out = list.slice(0, 40).filter((x) => x && KINDS.has(x.kind) && x.heading).map((x) => ({
    heading: s(x.heading, 200), kind: x.kind, words: Math.max(0, Math.round(Number(x.words) || 0)), excerpt: s(x.excerpt, 200) || "",
  }));
  return out.length ? out : null;
}

export const onRequestPost = handle(async ({ request, env }) => {
  const { proposals, sites } = await readJson(request, 4 * 1024 * 1024);
  if (!Array.isArray(proposals) || proposals.length > 1000) throw new HttpError(400, "proposals must be an array of at most 1000");
  if (!Array.isArray(sites) || !sites.every((x) => SITE_RE.test(x))) throw new HttpError(400, "invalid sites");
  const t = now();
  const ids = new Set(proposals.map((p) => String(p.id)));
  const stmts = [];
  for (const site of sites) {
    const open = await env.DB.prepare("SELECT id FROM cleanup_proposals WHERE site = ? AND status = 'proposed'").bind(site).all();
    for (const r of open.results) if (!ids.has(r.id)) stmts.push(env.DB.prepare("DELETE FROM cleanup_proposals WHERE id = ?").bind(r.id));
  }
  for (const p of proposals) {
    if (!/^[0-9a-f]{16}$/.test(String(p.id)) || !SITE_RE.test(String(p.site)) || !sites.includes(p.site)) continue;
    if (!/^https?:\/\//i.test(String(p.url))) continue;
    const secs = cleanSections(p.sections);
    if (!secs) continue;
    stmts.push(env.DB.prepare(
      `INSERT INTO cleanup_proposals (id, site, url, title, sections, words, status, created_at, updated_at)
       VALUES (?, ?, ?, ?, ?, ?, 'proposed', ?, ?)
       ON CONFLICT(id) DO UPDATE SET title = excluded.title, sections = excluded.sections, words = excluded.words,
         updated_at = excluded.updated_at
       WHERE cleanup_proposals.status = 'proposed'`
    ).bind(p.id, p.site, s(p.url, 600), s(p.title, 300), JSON.stringify(secs), secs.reduce((a, x) => a + x.words, 0), t, t));
  }
  for (let i = 0; i < stmts.length; i += 50) await env.DB.batch(stmts.slice(i, i + 50));
  return json({ ok: true, proposals: proposals.length });
});
