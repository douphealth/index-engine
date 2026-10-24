# -*- coding: utf-8 -*-
"""Indexation odds: a logistic model trained on this portfolio's own Google verdicts.

Every URL Google has judged (indexed vs crawled / discovered / unknown) is a training example. The model then:
  * scores every indexable page with P(indexed),
  * explains the score in plain words (the features pulling it down the most),
  * simulates actions (more links from indexed pages, de-duplication, more substance, sourcing claims, a manual
    request) and ranks them by expected gain = page value x rise in odds.
It is correlational, not causal, and reports its own held-out quality (AUC). When the data cannot support a model
(too few examples, or AUC < 0.6) it says so and falls back to conservative fixed estimates.
"""
import hashlib
import json
import math
from collections import defaultdict
from urllib.parse import urlsplit

import numpy as np

from .util import days_since, now_iso, parse_ts

POS = {"INDEXED"}
NEG = {"CRAWLED_NOT_INDEXED", "DISCOVERED_NOT_INDEXED", "UNKNOWN_TO_GOOGLE"}
SCORED = POS | NEG | {"GOOGLE_STALE", "GOOGLE_PICKED_OTHER", "NOT_INSPECTED"}

# name, phrase when it pulls the odds DOWN because it is low / because it is present
FEATURES = [
    ("site_rate", "the site as a whole is rarely indexed", None),
    ("inl_indexed", "few links from pages Google already indexes", None),
    ("inl_body", "few links from article text", None),
    ("inl_all", "few internal links overall", None),
    ("log_words", "little main content", None),
    ("near_dup", None, "near-duplicate of another page"),
    ("thin", None, "thin content"),
    ("trust", None, "unverified first-hand claims"),
    ("log_bing_impr", "no search demand seen on Bing", None),
    ("bing_click", "earns no Bing clicks", None),
    ("log_age", "very new", "old"),
    ("depth", "shallow URL", "deep URL"),
    ("in_sitemap", "missing from the sitemap", None),
    ("is_post", "not a blog post", "blog post"),
]
NAMES = [f[0] for f in FEATURES]
MIN_CLASS = 20
MIN_AUC = 0.60


def _link_counts(db):
    """{dst_without_slash: [all, body, from_indexed]} from pages that actually serve their own content."""
    counts = defaultdict(lambda: [set(), set(), set()])
    q = """SELECT l.src, l.dst, l.in_content, ts.bucket AS sb FROM links l
           JOIN audits a ON a.url=l.src AND a.status=200 AND a.hops=0 LEFT JOIN triage ts ON ts.url=l.src"""
    for r in db.execute(q):
        d = r["dst"].rstrip("/")
        c = counts[d]
        c[0].add(r["src"])
        if r["in_content"]:
            c[1].add(r["src"])
        if r["sb"] == "INDEXED":
            c[2].add(r["src"])
    return {k: [len(a), len(b), len(c)] for k, (a, b, c) in counts.items()}


def frame(db):
    """One record per scored URL: raw inputs plus the feature vector."""
    rows = db.execute("""
        SELECT t.url, t.site, t.bucket, t.flags, t.value, a.words, a.title, u.in_sitemap, u.kind, u.published, u.first_seen,
               u.lastmod, COALESCE(p.bing_impr,0) AS bing_impr, COALESCE(p.bing_clicks,0) AS bing_clicks,
               g.last_crawl
        FROM triage t JOIN urls u ON u.url=t.url LEFT JOIN audits a ON a.url=t.url LEFT JOIN perf p ON p.url=t.url
        LEFT JOIN gsc g ON g.url=t.url""").fetchall()
    links = _link_counts(db)
    known = defaultdict(lambda: [0, 0])
    for r in rows:
        if r["bucket"] in POS | NEG:
            known[r["site"]][0] += r["bucket"] in POS
            known[r["site"]][1] += 1
    site_rate = {s: (i + 1) / (n + 4) for s, (i, n) in known.items()}
    out = []
    for r in rows:
        if r["bucket"] not in SCORED:
            continue
        flags = set((r["flags"] or "").split(","))
        inl = links.get(r["url"].rstrip("/"), [0, 0, 0])
        age = days_since(r["published"] or r["first_seen"])
        raw = {"site_rate": site_rate.get(r["site"], 0.2), "inl_all": inl[0], "inl_body": inl[1], "inl_indexed": inl[2],
               "words": r["words"] or 0, "near_dup": int("NEAR_DUPLICATE" in flags), "thin": int("THIN" in flags),
               "trust": int("TRUST_RISK" in flags), "bing_impr": r["bing_impr"], "bing_click": int(r["bing_clicks"] > 0),
               "age": age if age is not None else 365, "depth": len([s for s in urlsplit(r["url"]).path.split("/") if s]),
               "in_sitemap": int(bool(r["in_sitemap"])), "is_post": int(r["kind"] == "post")}
        out.append({"url": r["url"], "site": r["site"], "bucket": r["bucket"], "flags": flags, "value": r["value"] or 0,
                    "title": r["title"] or "", "lastmod": r["lastmod"], "last_crawl": r["last_crawl"],
                    "ignored": "IGNORED" in flags, "raw": raw})
    return out


def vector(raw):
    return [raw["site_rate"], math.log1p(raw["inl_indexed"]), math.log1p(raw["inl_body"]), math.log1p(raw["inl_all"]),
            math.log1p(raw["words"]), raw["near_dup"], raw["thin"], raw["trust"], math.log1p(raw["bing_impr"]),
            raw["bing_click"], math.log1p(max(0, raw["age"])), raw["depth"], raw["in_sitemap"], raw["is_post"]]


def _sigmoid(z):
    return 1.0 / (1.0 + np.exp(-np.clip(z, -30, 30)))


def fit(X, y, l2=1.0, iters=25):
    """L2-regularised logistic regression by Newton's method on standardised features (intercept unpenalised)."""
    mu, sd = X.mean(0), X.std(0)
    sd[sd == 0] = 1.0
    Z = np.hstack([np.ones((len(X), 1)), (X - mu) / sd])
    w = np.zeros(Z.shape[1])
    reg = np.full(Z.shape[1], l2)
    reg[0] = 0.0
    for _ in range(iters):
        p = _sigmoid(Z @ w)
        g = Z.T @ (p - y) + reg * w
        H = (Z * (p * (1 - p))[:, None]).T @ Z + np.diag(reg) + 1e-9 * np.eye(len(w))
        step = np.linalg.solve(H, g)
        w -= step
        if np.abs(step).max() < 1e-6:
            break
    return {"w": w.tolist(), "mu": mu.tolist(), "sd": sd.tolist()}


def predict(m, X):
    X = np.atleast_2d(np.asarray(X, dtype=float))
    Z = np.hstack([np.ones((len(X), 1)), (X - np.array(m["mu"])) / np.array(m["sd"])])
    return _sigmoid(Z @ np.array(m["w"]))


def auc(y, p):
    """Probability a random indexed page scores above a random non-indexed one (ties count half)."""
    y, p = np.asarray(y), np.asarray(p)
    pos, neg = p[y == 1], p[y == 0]
    if not len(pos) or not len(neg):
        return None
    order = np.argsort(np.concatenate([pos, neg]), kind="mergesort")
    ranks = np.empty(len(order))
    vals = np.concatenate([pos, neg])[order]
    i = 0
    while i < len(vals):                         # average ranks for ties
        j = i
        while j + 1 < len(vals) and vals[j + 1] == vals[i]:
            j += 1
        ranks[order[i:j + 1]] = (i + j) / 2 + 1
        i = j + 1
    return float((ranks[:len(pos)].sum() - len(pos) * (len(pos) + 1) / 2) / (len(pos) * len(neg)))


def _fold(url, k=5):
    return int(hashlib.md5(url.encode()).hexdigest(), 16) % k


def cross_validate(X, y, sites, folds):
    """Out-of-fold predictions; returns (pooled AUC, within-site AUC). Within-site is what matters for choosing
    which pages of a site to work on: pooled AUC is flattered by big differences between sites."""
    oof = np.zeros(len(y))
    for f in sorted(set(folds)):
        test = folds == f
        if test.all() or len(set(y[~test])) < 2:
            return None, None
        oof[test] = predict(fit(X[~test], y[~test]), X[test])
    pooled = auc(y, oof)
    per, weight = [], []
    for s in sorted(set(sites)):
        idx = sites == s
        pos, n = int(y[idx].sum()), int(idx.sum())
        if pos >= 10 and n - pos >= 10:
            per.append(auc(y[idx], oof[idx]))
            weight.append(n)
    within = float(np.average(per, weights=weight)) if per else None
    return pooled, within


def train(records):
    lab = [r for r in records if r["bucket"] in POS | NEG]
    y = np.array([1.0 if r["bucket"] in POS else 0.0 for r in lab])
    meta = {"trained_at": now_iso(), "n": len(lab), "indexed": int(y.sum()), "features": NAMES}
    if y.sum() < MIN_CLASS or (len(y) - y.sum()) < MIN_CLASS:
        meta.update(quality="insufficient", note=f"needs at least {MIN_CLASS} indexed and {MIN_CLASS} non-indexed pages with Google verdicts")
        return None, meta
    X = np.array([vector(r["raw"]) for r in lab])
    pooled, within = cross_validate(X, y, np.array([r["site"] for r in lab]), np.array([_fold(r["url"]) for r in lab]))
    m = fit(X, y)
    meta["auc"] = round(pooled, 3) if pooled is not None else None
    meta["auc_within_site"] = round(within, 3) if within is not None else None
    judge = within if within is not None else pooled
    meta["quality"] = "good" if judge and judge >= 0.75 else "fair" if judge and judge >= MIN_AUC else "weak"
    m["names"] = NAMES
    # What drives indexing on THESE sites: standardised coefficients, strongest first.
    coef = sorted(zip(NAMES, m["w"][1:]), key=lambda kv: -abs(kv[1]))
    meta["drivers"] = [{"feature": k, "weight": round(v, 3), "direction": "raises" if v > 0 else "lowers"} for k, v in coef[:8]]
    if meta["quality"] == "weak":
        meta["note"] = "held-out AUC below 0.60: odds are shown as rough estimates only"
    return m, meta


def explain(m, x):
    """The (up to) three features pulling this page's odds down the most, in words."""
    z = (np.asarray(x) - np.array(m["mu"])) / np.array(m["sd"])
    contrib = np.array(m["w"][1:]) * z
    out = []
    for i in np.argsort(contrib):
        if contrib[i] > -0.15 or len(out) == 3:
            break
        name, low, present = FEATURES[i]
        phrase = (present if z[i] > 0 else low) or (low if z[i] <= 0 else present)
        if phrase:
            out.append(phrase)
    return out


ACTIONS = {
    "add_links": "Add 3 contextual links from related pages Google already indexes",
    "differentiate": "Make it clearly different from its near-duplicate, or merge the two (check Bing first)",
    "expand": "Add real substance (first-hand, sourced detail): the main content is short",
    "fix_trust": "Source or remove the flagged first-hand or statistical claims",
    "request": "Request indexing in Search Console",
}


def simulate(raw, action):
    r = dict(raw)
    if action == "add_links":
        r["inl_indexed"] += 3
        r["inl_body"] += 3
        r["inl_all"] += 3
    elif action == "differentiate":
        r["near_dup"] = 0
    elif action == "expand":
        r["words"], r["thin"] = max(r["words"], 1200), 0
    elif action == "fix_trust":
        r["trust"] = 0
    return r


# Conservative fixed rises when no trustworthy model exists (or for requests, before outcomes are measured).
FALLBACK_RISE = {"add_links": 0.08, "differentiate": 0.06, "expand": 0.05, "fix_trust": 0.04}


def score(db, request_rate=0.1, max_actions=80, per_site=12):
    """Train, write odds/why into triage, and return (model meta, best actions, {url: request gain},
    potential {site: {action: {"n": pages, "pages": expected extra indexed pages}}} over ALL candidates, not just the best)."""
    recs = frame(db)
    m, meta = train(recs)
    usable = m is not None and meta.get("quality") in ("good", "fair")
    X = np.array([vector(r["raw"]) for r in recs]) if recs else np.zeros((0, len(NAMES)))
    if m is not None:
        P = predict(m, X)
    else:
        P = np.array([r["raw"]["site_rate"] for r in recs])
    actions, request_gain = [], {}
    for r, x, p in zip(recs, X, P):
        why = explain(m, x) if m is not None else []
        db.execute("UPDATE triage SET odds=?, why=? WHERE url=?", (round(float(p), 4), json.dumps(why), r["url"]))
        if r["bucket"] in POS or r["ignored"]:
            continue
        weight = 1 + math.log1p(max(0.0, r["value"]))
        opts = []
        for a in ("add_links", "differentiate", "expand", "fix_trust"):
            if a == "differentiate" and not r["raw"]["near_dup"]:
                continue
            if a == "expand" and not (r["raw"]["thin"] or r["raw"]["words"] < 600):
                continue
            if a == "fix_trust" and not r["raw"]["trust"]:
                continue
            after = float(predict(m, [vector(simulate(r["raw"], a))])[0]) if usable else min(0.99, p + FALLBACK_RISE[a] * (1 - p))
            if after - p > 0.01:
                opts.append((a, after))
        lm, lc = parse_ts(r["lastmod"]), parse_ts(r["last_crawl"])
        eligible_request = r["bucket"] in ("UNKNOWN_TO_GOOGLE", "DISCOVERED_NOT_INDEXED", "GOOGLE_STALE") or (
            r["bucket"] == "CRAWLED_NOT_INDEXED" and lm and lc and lm > lc)
        if eligible_request and not ({"THIN", "NEAR_DUPLICATE", "SOFT_404"} & r["flags"]):
            after = p + request_rate * (1 - p)
            request_gain[r["url"]] = weight * (after - p)
            opts.append(("request", after))
        for a, after in opts:
            actions.append({"site": r["site"], "url": r["url"], "title": r["title"], "bucket": r["bucket"], "action": a,
                            "label": ACTIONS[a], "p_now": round(float(p), 3), "p_after": round(after, 3),
                            "gain": round(weight * (after - float(p)), 4)})
    db.commit()
    actions.sort(key=lambda a: -a["gain"])
    potential = defaultdict(lambda: defaultdict(lambda: {"n": 0, "pages": 0.0}))
    for a in actions:
        t = potential[a["site"]][a["action"]]
        t["n"] += 1
        t["pages"] = round(float(t["pages"] + a["p_after"] - a["p_now"]), 3)
    picked, n_site, seen = [], defaultdict(int), set()
    for a in actions:
        key = (a["url"], a["action"])
        if n_site[a["site"]] >= per_site or key in seen:
            continue
        seen.add(key)
        n_site[a["site"]] += 1
        picked.append(a)
        if len(picked) >= max_actions:
            break
    meta["used_for_actions"] = usable
    db.execute("DELETE FROM model")
    db.execute("INSERT INTO model VALUES(?,?)", (meta["trained_at"], json.dumps({"meta": meta, "model": m})))
    db.commit()
    return meta, picked, request_gain, {s: dict(v) for s, v in potential.items()}
