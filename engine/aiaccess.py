# -*- coding: utf-8 -*-
"""Can search engines and AI answer engines read each site? A daily, read-only check of robots.txt and of what the
site's edge (Cloudflare) answers when the home page is requested in each crawler's name.

Search crawlers decide whether a site can be found and cited: Googlebot and bingbot (Bing's index also feeds ChatGPT
search and Copilot), OAI-SearchBot and ChatGPT-User (ChatGPT search and browsing), Claude-SearchBot and Claude-User,
PerplexityBot and Perplexity-User. Blocking any of these removes the site from that engine's answers.
Training crawlers (GPTBot, ClaudeBot, Google-Extended, Applebot-Extended, CCBot, meta-externalagent) only collect text for
model training. Blocking them is a legitimate choice and does not remove the site from search or AI answers
(Google-Extended does not affect Google Search or AI Overviews).

robots.txt is authoritative. The live request is evidence, not proof: it comes from this PC, not from the crawler's own
network, so an edge that allows the real crawler may still challenge an impostor. A 200 is conclusive; a 403 or a
challenge means "check the site's bot settings" (Cloudflare: Security > Bots / AI Crawl Control).
"""
import time
from urllib import robotparser

import requests

from . import alerts, config
from .util import log, now_iso

BOTS = [   # (name, role, User-Agent used for the live request or None for robots.txt only)
    ("Googlebot", "search", None),
    ("bingbot", "search", None),
    ("OAI-SearchBot", "search",
     "Mozilla/5.0 AppleWebKit/537.36 (KHTML, like Gecko); compatible; OAI-SearchBot/1.0; +https://openai.com/searchbot"),
    ("ChatGPT-User", "search",
     "Mozilla/5.0 AppleWebKit/537.36 (KHTML, like Gecko); compatible; ChatGPT-User/1.0; +https://openai.com/bot"),
    ("Claude-SearchBot", "search", None),
    ("Claude-User", "search", None),
    ("PerplexityBot", "search",
     "Mozilla/5.0 AppleWebKit/537.36 (KHTML, like Gecko; compatible; PerplexityBot/1.0; +https://perplexity.ai/perplexitybot)"),
    ("Perplexity-User", "search", None),
    ("GPTBot", "training",
     "Mozilla/5.0 AppleWebKit/537.36 (KHTML, like Gecko); compatible; GPTBot/1.1; +https://openai.com/gptbot"),
    ("ClaudeBot", "training",
     "Mozilla/5.0 AppleWebKit/537.36 (KHTML, like Gecko; compatible; ClaudeBot/1.0; +claudebot@anthropic.com)"),
    ("Google-Extended", "training", None),
    ("Applebot-Extended", "training", None),
    ("CCBot", "training", None),
    ("meta-externalagent", "training", None),
]
BROWSER = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36"
PAUSE_S = 0.3


def _get(session, url, ua):
    try:
        r = session.get(url, headers={"User-Agent": ua}, timeout=30, allow_redirects=True)
    except requests.RequestException:
        return None, False
    challenged = bool(r.headers.get("cf-mitigated")) or (r.status_code in (403, 429, 503) and b"Just a moment" in r.content[:5000])
    return r.status_code, challenged


def state_of(robots_ok, http, challenged, baseline_ok):
    if not robots_ok:
        return "blocked_robots"
    if http is None or not baseline_ok:
        return "allowed"                     # robots.txt allows it; the live request said nothing (or the site was down)
    if challenged or http in (401, 403, 429, 503):
        return "blocked_edge"
    return "allowed"


def check_site(site, session=None):
    """[{bot, role, robots_ok, http, state}] for one site."""
    session = session or requests.Session()
    base = config.site_base(site).rstrip("/") + "/"
    try:
        r = session.get(base + "robots.txt", headers={"User-Agent": config.UA}, timeout=30)
        txt = r.text if r.status_code == 200 else ""
    except requests.RequestException:
        txt = ""
    rp = robotparser.RobotFileParser()
    rp.parse(txt.splitlines())
    baseline, _ = _get(session, base, BROWSER)
    baseline_ok = baseline is not None and baseline < 400
    out = []
    for name, role, ua in BOTS:
        robots_ok = rp.can_fetch(name, base)
        http, challenged = (None, False)
        if ua and robots_ok and baseline_ok:
            http, challenged = _get(session, base, ua)
            time.sleep(PAUSE_S)
        out.append({"bot": name, "role": role, "robots_ok": int(robots_ok), "http": http,
                    "state": state_of(robots_ok, http, challenged, baseline_ok)})
    return out, baseline_ok


def run(db, sites, session=None):
    """Check every site, store the result and raise an alert when a crawler's access changes."""
    session = session or requests.Session()
    for site in sites:
        rows, baseline_ok = check_site(site, session)
        if not baseline_ok:
            log(f"[ai-access] {site}: home page did not answer normally; crawler access not judged this run")
            continue
        prev = {r["bot"]: r["state"] for r in db.execute("SELECT bot, state FROM ai_access WHERE site=?", (site,))}
        now = now_iso()
        newly_blocked, unblocked, training = [], [], []
        for r in rows:
            db.execute("INSERT OR REPLACE INTO ai_access(site, bot, role, robots_ok, http, state, checked_at) VALUES(?,?,?,?,?,?,?)",
                       (site, r["bot"], r["role"], r["robots_ok"], r["http"], r["state"], now))
            before = prev.get(r["bot"])
            if before is None or before == r["state"]:
                continue                     # first check, or no change: nothing to report
            if r["role"] == "training":
                training.append(r)
            elif r["state"] != "allowed":
                newly_blocked.append(r)
            else:
                unblocked.append(r)
        if newly_blocked:
            parts = [f"{r['bot']} ({_engine(r['bot'])}) by {'robots.txt' if r['state'] == 'blocked_robots' else 'the site edge'}"
                     for r in newly_blocked]
            alerts.add(db, site, None, "ai_search_blocked", "high",
                       "Search crawlers newly blocked: " + "; ".join(parts) + ". Blocked crawlers cannot find or cite the site. "
                       "Check robots.txt, then Cloudflare > Security > Bots / AI Crawl Control.")
        if unblocked:
            alerts.add(db, site, None, "ai_search_unblocked", "good",
                       "Search crawlers that can read the site again: " + ", ".join(r["bot"] for r in unblocked) + ".")
        if training:
            alerts.add(db, site, None, "ai_training_access", "info",
                       "Training-crawler access changed (these do not affect search or AI answers): "
                       + ", ".join(f"{r['bot']} now {'allowed' if r['state'] == 'allowed' else 'blocked'}" for r in training) + ".")
        blocked = [r["bot"] for r in rows if r["role"] == "search" and r["state"] != "allowed"]
        log(f"[ai-access] {site}: search crawlers " + (f"BLOCKED: {', '.join(blocked)}" if blocked else "all allowed")
            + f"; training crawlers blocked: {sum(1 for r in rows if r['role'] == 'training' and r['state'] != 'allowed')}")
    db.commit()


def _engine(bot):
    return {"Googlebot": "Google Search and AI Overviews", "bingbot": "Bing, Copilot and ChatGPT search results from Bing",
            "OAI-SearchBot": "ChatGPT search", "ChatGPT-User": "ChatGPT browsing", "Claude-SearchBot": "Claude's web search",
            "Claude-User": "Claude's web browsing", "PerplexityBot": "Perplexity answers",
            "Perplexity-User": "Perplexity browsing"}.get(bot, "search answers")


def summary(db, site):
    rows = db.execute("SELECT bot, role, robots_ok, http, state, checked_at FROM ai_access WHERE site=? ORDER BY role, bot",
                      (site,)).fetchall()
    if not rows:
        return None
    return {"checked_at": max(r["checked_at"] for r in rows),
            "search_blocked": [r["bot"] for r in rows if r["role"] == "search" and r["state"] != "allowed"],
            "training_blocked": [r["bot"] for r in rows if r["role"] == "training" and r["state"] != "allowed"],
            "bots": [dict(r) for r in rows]}
