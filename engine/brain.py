# -*- coding: utf-8 -*-
"""Analysis, offline (local data only): triage -> odds model -> request list ranked by expected gain ->
best next actions -> measured impact of past actions."""
from . import impact, model, revival, traffic, triage


def analyze(db, sites):
    results = triage.run(db, sites, with_manual=False)
    meta, actions, gains, potential = model.score(db, request_rate=impact.request_rate(db))
    for site, res in results.items():
        res["manual"] = triage.manual_queue(db, site, res["rows"], res["buckets"], res["owner"], gains["request"])
        res["traffic"] = traffic.site_traffic(db, site)
        res["health"] = traffic.bing_health(db, site)
        res["data_health"] = traffic.data_health(db, site)
        res["revival"] = revival.analyze(db, site, res["traffic"])
        res["revival"]["headline"] = revival.headline(site, res["traffic"], res["revival"], res["health"])
    db.commit()
    return results, {"model": meta, "actions": actions, "potential": potential, "link_gain": gains["add_links"],
                     "impact": impact.compute(db)}
