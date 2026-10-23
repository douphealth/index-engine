// Owner decisions on link proposals. The engine on the PC applies approved ones and undoes on request.
import { handle, HttpError, json, now, readJson } from "../../lib/http.js";

const TRANSITIONS = {           // action: [allowed from-statuses, to-status]
  approve: [["proposed", "failed", "rejected"], "approved"],
  reject: [["proposed", "approved"], "rejected"],
  cancel: [["approved"], "proposed"],
  undo: [["applied"], "undo"],
  reopen: [["rejected", "failed", "undone"], "proposed"],
};

export const onRequestPost = handle(async ({ request, env }) => {
  const { id, ids, action } = await readJson(request);
  const tr = TRANSITIONS[action];
  if (!tr) throw new HttpError(400, "unknown action");
  const list = (Array.isArray(ids) ? ids : [id]).map(String).slice(0, 100);
  const [from, to] = tr;
  const ph = from.map(() => "?").join(",");
  const stmts = list.map((x) => env.DB.prepare(
    `UPDATE link_proposals SET status = ?, updated_at = ? WHERE id = ? AND status IN (${ph})`).bind(to, now(), x, ...from));
  const res = stmts.length ? await env.DB.batch(stmts) : [];
  return json({ ok: true, changed: res.reduce((a, r) => a + (r.meta.changes || 0), 0), status: to });
});
