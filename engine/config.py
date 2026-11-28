# -*- coding: utf-8 -*-
"""Settings for the index engine. Edit SITES to add or remove a site."""
import os
import re
from pathlib import Path

APP_DIR = Path(__file__).resolve().parents[1]
PORTFOLIO = APP_DIR.parent
OUT = APP_DIR / "out"
DB_PATH = OUT / "index_engine.db"
LOG_DIR = OUT / "logs"

# Same credential file the other portfolio scripts read (GSC service account JSON + Bing key).
CRED_FILE = Path(os.environ.get("INDEX_ENGINE_CREDS", PORTFOLIO / "GSC-Bing.txt"))
# Online dashboard connection, kept with the other credential files and never in the repo:
# {"url": "https://<project>.pages.dev", "token": "<ENGINE_TOKEN>"}
DASHBOARD_FILE = Path(os.environ.get("INDEX_ENGINE_DASHBOARD_FILE", PORTFOLIO / "Index-Engine-Dashboard.json"))
DAILY_RUN_TIME = "10:30"        # local time; the dashboard setting overrides it
# The app's own Cloudflare token (deploys; clears the edge cache of a page after a link change when it has Cache Purge).
CF_TOKEN_FILE = Path(os.environ.get("INDEX_ENGINE_CF_TOKEN_FILE", PORTFOLIO / "Index-Engine-Cloudflare-Token.txt"))
LOCK_FILE = OUT / "run.lock"
LOCK_STALE_HOURS = 4
# Optional: {"example.com": {"key": "...", "keyLocation": "https://example.com/<key>.txt"}}
INDEXNOW_KEYS_FILE = APP_DIR / "indexnow_keys.json"
# Automatic onboarding (engine/onboard.py): Cloudflare tokens that may write DNS, read in this order (missing files are
# skipped), and local settings (owners to add in Search Console, the Bing account code). Neither is in git.
DNS_TOKEN_FILES = [Path(p) for p in os.environ.get("INDEX_ENGINE_DNS_TOKEN_FILES", "").split(os.pathsep) if p] or [
    PORTFOLIO / "Index-Engine-Cloudflare-DNS-Token.txt", PORTFOLIO / "Cloudflare_API-Token.txt", CF_TOKEN_FILE]
ONBOARDING_FILE = APP_DIR / "onboarding.json"
ONBOARD_RETRY_HOURS = 6         # a site that could not be onboarded is tried again after this long
HOLDOUT_PCT = 10                # randomized control group (engine/holdout.py): share of eligible pages held back; 0 = off

SITES = [
    "gearuptofit.com",
    "affiliatemarketingforsuccess.com",
    "mysticaldigits.com",
    "frenchyfab.com",
    "micegoneguide.com",
    "gearuptogrow.com",
    "plantastichaven.com",
    "efficientgptprompts.com",
    "openclaw-skillshub.com",
    "imagealchemy.app",
    # Added 25 Nov 2026 (owner's list). Cloudflare-hosted sites unless noted; no WordPress login on the PC yet.
    "outdoormisting.com",        # WordPress (CyberPanel)
    "tenderatlas.shop",
    "signalroom.store",
    "stainsolver.shop",
    "borderwise.online",
    "checkthetag.shop",
    "solarpayback.shop",
    "stopthat.shop",
    "buyonce.site",
    "cabinvolt.shop",
    "bordercover.shop",
    "fixorrefund.shop",
]

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/124 Safari/537.36 PortfolioIndexEngine/1.0")

# ---------------------------------------------------------------- live audit
PER_HOST_CONCURRENCY = 3        # requests in flight per site at the start of an audit
# A site that keeps answering fast gets one more request in flight at a time, up to this many; a slow answer, an error,
# a 429/5xx or a bot challenge halves it at once and caps it there for the rest of the run (engine/audit.py Pace).
PER_HOST_MAX = 8
AUDIT_FAST_MS = 800             # an answer within this counts as fast ...
AUDIT_RAMP_AFTER = 8            # ... and this many fast answers in a row earn one more request in flight
AUDIT_SLOW_MS = 5_000           # an answer slower than this means the site is busy: back off
SLOW_HOSTS = {"mysticaldigits.com": 2}   # uncached pages take up to ~60 s (Sep 2026 audit); never raised
CONNECT_TIMEOUT, READ_TIMEOUT = 15, 90
AUDIT_MAX_AGE_DAYS = 7          # re-fetch a page after this, or sooner if its sitemap lastmod moves
# Most pages one run fetches from one site (an explicit --audit-max or --force-audit overrides it). On 25 Nov 2026
# the first audit of tenderatlas.shop fetched 3,313 pages in ten minutes; every page costs that site database reads,
# its daily allowance ran out part-way, and 2,102 pages then answered 404. A large site is now read over several runs.
AUDIT_MAX_PER_SITE = 500
# Sites read at the same time by the stages that ask one question per site (sitemaps, search data, crawler access).
# Each site is still asked one thing at a time; only different sites overlap. Bing's API is asked more gently.
FETCH_WORKERS = 6
BING_WORKERS = 4
WP_WORKERS = 4                  # WordPress sites whose changed posts are read at once (several may share a server)
# tenderatlas.shop has a metered database behind every page (Cloudflare D1 free plan, 5M rows read a day). It was held
# to 150 until its fix of 26 Nov 2026 was measured: a page now costs about 40 rows (600+ before), so 500 pages are
# about 20,000 rows, 0.4% of a day. Kept as an entry so the number is one edit away if that site's cost changes.
AUDIT_SITE_MAX = {"tenderatlas.shop": 500}
AUDIT_RECHECK_SHARE = 1 / 3     # of a capped run, kept for pages due a re-read when new pages alone would fill it
AUDIT_RECHECK_BROKEN_DAYS = 1   # a page the sitemap lists but that answered 4xx is fetched again after this, not a week
# This many failed answers in a row (404, 410, 429, 5xx, no answer), after the site had answered normally in the same
# run, stops that site's audit for the run: the site is failing, and more requests only add to it.
AUDIT_STOP_AFTER = 12
AUDIT_STOP_MIN_OK = 5
SLOW_MS = 10_000                # pages slower than this are a crawl-rate risk
THIN_WORDS = 250                # main-content words below this = thin
HEAVY_PAGE_BYTES = 1_000_000    # HTML served above this is a heavy page (this portfolio's normal article is 150-250 KB)
HEAVY_B64_CHARS = 20_000        # embedded base64 image data above this makes a page heavy on its own

# ---------------------------------------------------------------- Google
# URL Inspection API: 2,000 queries/day and 600/minute per property
# (developers.google.com/webmaster-tools/limits, checked 23 Oct 2026). Day = Pacific time.
INSPECT_DAILY_LIMIT = 2000
INSPECT_SAFETY_MARGIN = 50
# One inspection takes Google several seconds to answer, so the calls in flight decide the speed, not the per-minute
# cap. Measured on one property on 26 Nov 2026: 4 in flight, 0.53 URLs a second (6.6 s a call); 12 in flight, 1.31 a
# second (11 s a call: Google slows each call as they pile up, so more than this buys little). The pacing in
# google.inspect_due still keeps every property under 600 a minute whatever this is.
INSPECT_WORKERS_PER_PROPERTY = 12
INSPECT_WORKERS_MAX = 64        # threads in total, however many properties have work
# Re-inspect a URL only after this many days in its last state.
REINSPECT_DAYS = {"indexed": 45, "crawled": 14, "discovered": 7, "unknown": 7, "other": 10, "error": 2}
EVIDENCE_DAYS = 14              # a page with Search impressions in this window is indexed; skip inspecting it
PERF_DAYS = 90

# ---------------------------------------------------------------- submission
INDEXNOW_ENDPOINT = "https://api.indexnow.org/indexnow"
INDEXNOW_MAX_PER_POST = 10_000  # indexnow.org/documentation
BING_BATCH = 100
RESUBMIT_UNCHANGED_DAYS = 30    # never re-send an unchanged URL on the same channel sooner than this
SUBMIT_KINDS = {"post", "page", "product", "other"}   # taxonomy archives are skipped unless --include-taxonomy

# Sitemaps and the RSS feed, registered with Google and Bing (engine/sitemaps.py).
SITEMAP_RESUBMIT_MIN_HOURS = 1  # a registered sitemap is resubmitted after URLs were added or updated, at most this often
SITEMAP_DENIED_RETRY_DAYS = 7   # after a 401/403 (no permission), wait this long before trying that sitemap again
WEBSUB_MIN_MINUTES = 10         # at most one WebSub publish notice per site per tick

# Sites whose own rules require their gate before any submission (gearuptofit AGENTS.md rule 4).
# The script is called as `python <gate> gate --file <list>` and only its PASS lines are submitted.
SITE_GATES = {"gearuptofit.com": PORTFOLIO / "01-gearuptofit" / "site_guard" / "site_guard.py"}

# Published pages a site leaves out of its sitemaps on purpose; never flagged as "missing from sitemap".
INTENTIONAL_OUT_OF_SITEMAP = {
    # gearuptofit Worker EXCLUDED_PAGE_SITEMAP_PATHS: hub pages whose canonical home is the category (checked 23 Oct 2026)
    "https://gearuptofit.com/weight-loss/", "https://gearuptofit.com/video-workout/",
}

# Sites whose published-but-redirected posts are documented as deliberate (gearuptofit AGENTS.md: 33 legacy URLs).
DELIBERATE_REDIRECT_SITES = {"gearuptofit.com"}

# Manual "Request indexing" in the Search Console UI has no API and a small daily cap per property.
MANUAL_QUEUE_PER_SITE = 10       # Search Console's own limit is about 10 manual requests a day per property
MANUAL_QUEUE_TOTAL = 10          # across all sites: the owner's daily list (dashboard setting manual_queue_total)
MANUAL_REQUEUE_DAYS = 14


DOMAIN_RE = re.compile(r"^(?=.{4,80}$)([a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,24}$")


def clean_domain(value):
    """'https://www.Example.com/path' -> 'www.example.com'; None when it is not a plain public domain name."""
    d = str(value or "").strip().lower()
    d = re.sub(r"^[a-z]+://", "", d).split("/")[0].split("?")[0].split("#")[0].split(":")[0].rstrip(".")
    return d if DOMAIN_RE.match(d) else None


def add_sites(extra):
    """Websites the owner added in the dashboard (Settings > Websites), appended to SITES in place so every module that
    reads config.SITES sees them. Returns the ones added now."""
    added = []
    for v in extra or []:
        d = clean_domain(v)
        if d and d not in SITES and (d[4:] if d.startswith("www.") else "www." + d) not in SITES:
            SITES.append(d)
            added.append(d)
    return added


def site_base(site):
    """'example.com' -> 'https://example.com'; a full base URL is kept as is (used by the selftest)."""
    site = site.rstrip("/")
    return site if "://" in site else "https://" + site
