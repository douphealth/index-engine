# -*- coding: utf-8 -*-
"""Analysis, offline (local data only): triage -> odds model -> request list ranked by expected gain ->
best next actions -> measured impact of past actions."""
from . import impact, model, triage


def analyze(db, sites):
    results = triage.run(db, sites, with_manual=False)
    meta, actions, request_gain = model.score(db, request_rate=impact.request_rate(db))
    for site, res in results.items():
        res["manual"] = triage.manual_queue(db, site, res["rows"], res["buckets"], res["owner"], request_gain)
    db.commit()
    return results, {"model": meta, "actions": actions, "impact": impact.compute(db)}
