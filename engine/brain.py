# -*- coding: utf-8 -*-
"""Analysis, offline (local data only): triage -> odds model -> request list ranked by expected gain ->
best next actions -> measured impact of past actions."""
from . import aiaccess, impact, linkgaps, model, opportunities, recovery, results as measured, revival, traffic, triage


def analyze(db, sites):
    results = triage.run(db, sites, with_manual=False)
    meta, actions, gains, potential = model.score(db, request_rate=impact.request_rate(db))
    prior = opportunities.portfolio_curves(db)
    for site, res in results.items():
        res["opportunities"] = opportunities.analyze(db, site, res["buckets"], prior=prior)
        res["ai_access"] = aiaccess.summary(db, site)
        res["link_gaps"] = linkgaps.analyze(db, site, res["buckets"])
        res["manual"] = triage.manual_queue(db, site, res["rows"], res["buckets"], res["owner"], gains["request"])
        res["traffic"] = traffic.site_traffic(db, site)
        res["health"] = traffic.bing_health(db, site)
        res["data_health"] = traffic.data_health(db, site)
        res["revival"] = revival.analyze(db, site, res["traffic"])
        res["revival"]["headline"] = revival.headline(site, res["traffic"], res["revival"], res["health"])
        res["recovery"] = recovery.analyze(db, site, res)
        flagged = [x["url"] for x in res["recovery"]["plan"] if x["artifacts"]]
        for url in flagged:                        # visible on the Site page filter like every other flag
            bucket, flags = res["buckets"][url]
            if "TEMPLATE_ARTIFACT" not in flags:
                res["buckets"][url] = (bucket, list(flags) + ["TEMPLATE_ARTIFACT"])
                db.execute("UPDATE triage SET flags = CASE WHEN flags = '' THEN 'TEMPLATE_ARTIFACT' ELSE flags || ',TEMPLATE_ARTIFACT' END "
                           "WHERE url=?", (url,))
    db.commit()
    learned = measured.summary(db)
    learned["calibration"] = measured.calibration(db, learned)
    return results, {"results": learned, "model": meta, "actions": actions, "potential": potential, "link_gain": gains["add_links"],
                     "impact": impact.compute(db)}
