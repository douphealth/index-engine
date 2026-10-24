# -*- coding: utf-8 -*-
"""Settings for the index engine. Edit SITES to add or remove a site."""
import os
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
]

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/124 Safari/537.36 PortfolioIndexEngine/1.0")

# ---------------------------------------------------------------- live audit
PER_HOST_CONCURRENCY = 3
SLOW_HOSTS = {"mysticaldigits.com": 2}   # uncached pages take up to ~60 s (Sep 2026 audit)
CONNECT_TIMEOUT, READ_TIMEOUT = 15, 90
AUDIT_MAX_AGE_DAYS = 7          # re-fetch a page after this, or sooner if its sitemap lastmod moves
SLOW_MS = 10_000                # pages slower than this are a crawl-rate risk
THIN_WORDS = 250                # main-content words below this = thin

# ---------------------------------------------------------------- Google
# URL Inspection API: 2,000 queries/day and 600/minute per property
# (developers.google.com/webmaster-tools/limits, checked 23 Oct 2026). Day = Pacific time.
INSPECT_DAILY_LIMIT = 2000
INSPECT_SAFETY_MARGIN = 50
INSPECT_WORKERS_PER_PROPERTY = 4
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
MANUAL_QUEUE_PER_SITE = 10
MANUAL_REQUEUE_DAYS = 14


def site_base(site):
    """'example.com' -> 'https://example.com'; a full base URL is kept as is (used by the selftest)."""
    site = site.rstrip("/")
    return site if "://" in site else "https://" + site
