# -*- coding: utf-8 -*-
"""Randomized holdout: a true control group for the engine's links and cleanups.

engine/results.py compares a linked page with similar pages that got no link. That is observational: the engine chose
which pages to link, so its picks may differ from the controls in ways the data does not show. Here the choice is
taken out of the engine's hands. When a page first becomes eligible for a kind of change (a link proposal can be made
for it; a post has scaffolding to cut), a hash of (kind, site, URL) puts it in one of two arms, once and for good:

  * hold (HOLDOUT_PCT of pages, 10% by default): no change of that kind for HOLD_DAYS days;
  * treat: proposed (and applied, under the autopilot or after approval) as usual.

The hash is fixed, so the assignment does not depend on anything the engine knows about the page, and it never moves
a page between arms. After HOLD_DAYS a held page is released and treated like any other. The outcome is measured per
arm for pages that were not in Google's index when they were enrolled: the share Google indexed within OUTCOME_DAYS.
Because some treat-arm pages wait for approval or a daily cap, the comparison is "offered the change" against "held
back" (intention to treat): if anything it understates the effect of the change itself.
"""
import datetime as dt
import hashlib
import math

from . import config
from .util import norm, now_iso, parse_ts

HOLD_DAYS = 31
OUTCOME_DAYS = 30
MIN_ARM = 15                   # pages measured in the smaller arm before the result counts
NOT_INDEXED = ("crawled", "discovered", "unknown")


BUCKET_STATE = {"INDEXED": "indexed", "CRAWLED_NOT_INDEXED": "crawled", "DISCOVERED_NOT_INDEXED": "discovered",
                "UNKNOWN_TO_GOOGLE": "unknown"}


def states(buckets, urls):
    """{url: Google state} from the triage buckets, for assign()."""
    return {u: BUCKET_STATE.get((buckets.get(u) or (None,))[0]) for u in urls}


def arm(kind, site, url, pct):
    """'hold' or 'treat' from a fixed hash: pure, the same answer on every run and every PC."""
    h = int(hashlib.sha1(f"holdout|{kind}|{site}|{norm(url)}".encode()).hexdigest()[:8], 16) % 100
    return "hold" if h < pct else "treat"


def assign(db, kind, site, urls, states=None, pct=None, now=None):
    """Enrol pages seen eligible for the first time, and return the ones held back right now.
    states: {url: Google state when enrolled ('indexed', 'crawled', 'discovered', 'unknown' or None)}."""
    pct = config.HOLDOUT_PCT if pct is None else pct
    now = now or dt.datetime.now(dt.timezone.utc)
    urls = list(dict.fromkeys(urls))
    if not urls:
        return set()
    have = {}
    for i in range(0, len(urls), 500):
        chunk = urls[i:i + 500]
        q = ",".join("?" * len(chunk))
        for r in db.execute(f"SELECT url, arm, since FROM experiment WHERE kind=? AND url IN ({q})", (kind, *chunk)):
            have[r["url"]] = (r["arm"], r["since"])
    held = set()
    stamp = now.isoformat(timespec="seconds")
    for u in urls:
        if u in have:
            a, since = have[u]
        else:
            if pct <= 0:
                continue                                    # holdout switched off: nothing is enrolled
            a, since = arm(kind, site, u, pct), stamp
            db.execute("INSERT OR IGNORE INTO experiment(kind, site, url, arm, since, state) VALUES(?,?,?,?,?,?)",
                       (kind, site, u, a, since, (states or {}).get(u)))
        t = parse_ts(since)
        if a == "hold" and t and (now - t).days < HOLD_DAYS:
            held.add(u)
    return held


def _z_p(x1, n1, x2, n2):
    """Two-sided p-value of a two-proportion z-test (pooled). None when it cannot be computed."""
    if not n1 or not n2:
        return None
    p = (x1 + x2) / (n1 + n2)
    se = math.sqrt(p * (1 - p) * (1 / n1 + 1 / n2))
    if se == 0:
        return 1.0
    z = abs(x1 / n1 - x2 / n2) / se
    return round(math.erfc(z / math.sqrt(2)), 4)


def summary(db, now=None):
    """{kind: {...}} per kind of change: pages enrolled per arm, and once measurable, the share indexed per arm,
    the difference (lift) and its p-value. 'ready' says whether the smaller arm has MIN_ARM measured pages."""
    from .results import _history, _indexed_between
    now = now or dt.datetime.now(dt.timezone.utc)
    rows = db.execute("SELECT kind, url, arm, since, state FROM experiment").fetchall()
    if not rows:
        return {}
    hist = _history(db)
    out = {}
    for r in rows:
        k = out.setdefault(r["kind"], {"enrolled": {"treat": 0, "hold": 0}, "treat": [0, 0], "hold": [0, 0], "next": None})
        k["enrolled"][r["arm"]] += 1
        since = parse_ts(r["since"])
        if not since or r["state"] not in NOT_INDEXED:
            continue
        end = since + dt.timedelta(days=OUTCOME_DAYS)
        if end + dt.timedelta(days=3) > now:                # Google's data trails by a few days
            k["next"] = min(k["next"] or end.date().isoformat(), end.date().isoformat())
            continue
        a = k[r["arm"]]
        a[0] += 1
        a[1] += 1 if _indexed_between(hist.get(r["url"], []), since, end) else 0
    for k in out.values():
        (nt, it), (nh, ih) = k["treat"], k["hold"]
        k["treat"] = {"n": nt, "indexed": it, "rate": round(it / nt, 3) if nt else None}
        k["hold"] = {"n": nh, "indexed": ih, "rate": round(ih / nh, 3) if nh else None}
        k["lift"] = round(it / nt - ih / nh, 3) if nt and nh else None
        k["p_value"] = _z_p(it, nt, ih, nh)
        k["ready"] = min(nt, nh) >= MIN_ARM
    out["_meta"] = {"pct": config.HOLDOUT_PCT, "hold_days": HOLD_DAYS, "outcome_days": OUTCOME_DAYS, "min_arm": MIN_ARM,
                    "at": now_iso()}
    return out
