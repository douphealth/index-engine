# -*- coding: utf-8 -*-
"""Logging with secret scrubbing, credential loading, HTTP sessions, URL and time helpers."""
import datetime as dt
import json
import re
import sys
import threading
from urllib.parse import urlsplit, urlunsplit

import requests

from . import config

SECRETS = []
_log_fh = None
_log_lock = threading.Lock()
LOG_PATH = None


def scrub(s):
    s = str(s)
    for sec in SECRETS:
        if sec and len(sec) >= 6:
            s = s.replace(sec, "***")
    return s


def open_log(name):
    global _log_fh, LOG_PATH
    config.LOG_DIR.mkdir(parents=True, exist_ok=True)
    LOG_PATH = config.LOG_DIR / f"{name}-{dt.datetime.now():%Y%m%d-%H%M%S}.txt"
    _log_fh = open(LOG_PATH, "w", encoding="utf-8")


def log_text():
    """Everything logged since open_log (already scrubbed)."""
    try:
        return LOG_PATH.read_text(encoding="utf-8") if LOG_PATH else ""
    except OSError:
        return ""


def close_log():
    global _log_fh
    if _log_fh:
        _log_fh.close()
        _log_fh = None


def log(msg=""):
    msg = scrub(msg)
    with _log_lock:
        try:
            print(msg, flush=True)
        except UnicodeEncodeError:
            print(msg.encode("ascii", "replace").decode(), flush=True)
        if _log_fh:
            _log_fh.write(msg + "\n")
            _log_fh.flush()


# ---------------------------------------------------------------- credentials
# Same parser as portfolio-audit-2026-09-17/export_gsc_bing.py, so one file serves every script.

def load_service_account(raw):
    for text in (raw, raw.replace('""', '"')):
        text = text.replace("“", '"').replace("”", '"')
        dec = json.JSONDecoder()
        for m in re.finditer(r"\{", text):
            try:
                obj, _ = dec.raw_decode(text[m.start():])
            except ValueError:
                continue
            if isinstance(obj, dict) and obj.get("type") == "service_account" and obj.get("private_key"):
                return obj
    return None


def load_bing_key(raw):
    for line in raw.splitlines():
        if "bing" in line.lower():
            m = re.search(r"\b([0-9a-fA-F]{32})\b", line)
            if m:
                return m.group(1)
    m = re.search(r"(?is)bing.{0,300}?\b([0-9a-fA-F]{32})\b", raw)
    return m.group(1) if m else None


def bing_key_candidates(raw):
    """Every 32-hex token in the file, the one the old parser picks first. The file can hold a stale key next
    to the current one, so the caller validates each against the API instead of trusting position."""
    first = load_bing_key(raw)
    found = re.findall(r"\b[0-9a-fA-F]{32}\b", raw)
    return list(dict.fromkeys(([first] if first else []) + found))


def load_credentials(path=None):
    """Return (service_account_dict_or_None, [bing_key_candidates]). Never prints either."""
    path = path or config.CRED_FILE
    try:
        raw = open(path, encoding="utf-8", errors="replace").read()
    except OSError:
        return None, []
    sa, keys = load_service_account(raw), bing_key_candidates(raw)
    if sa:
        SECRETS.append(sa.get("private_key_id", ""))
        SECRETS.append(sa.get("private_key", ""))
    SECRETS.extend(keys)
    return sa, keys


# ---------------------------------------------------------------- HTTP

_tls = threading.local()


def http():
    """One requests.Session per thread."""
    s = getattr(_tls, "session", None)
    if s is None:
        s = requests.Session()
        s.headers.update({"User-Agent": config.UA, "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"})
        adapter = requests.adapters.HTTPAdapter(pool_connections=20, pool_maxsize=20, max_retries=0)
        s.mount("http://", adapter)
        s.mount("https://", adapter)
        _tls.session = s
    return s


def get(url, timeout=None, **kw):
    return http().get(url, timeout=timeout or (config.CONNECT_TIMEOUT, config.READ_TIMEOUT), **kw)


# ---------------------------------------------------------------- URLs

def host_of(url):
    return urlsplit(url).netloc.lower()


def bare_host(host):
    host = host.lower()
    return host[4:] if host.startswith("www.") else host


def same_site(url, site_host):
    return bare_host(host_of(url)) == bare_host(site_host)


def norm(url, base=None):
    """Canonical form for comparisons: lowercase scheme/host, no fragment, no query, no default port."""
    if not url:
        return ""
    url = url.strip()
    if base and not re.match(r"^[a-z][a-z0-9+.-]*:", url, re.I):
        from urllib.parse import urljoin
        url = urljoin(base, url)
    p = urlsplit(url)
    if p.scheme not in ("http", "https"):
        return ""
    netloc = p.netloc.lower()
    if netloc.endswith(":443") and p.scheme == "https":
        netloc = netloc[:-4]
    if netloc.endswith(":80") and p.scheme == "http":
        netloc = netloc[:-3]
    path = p.path or "/"
    return urlunsplit((p.scheme.lower(), netloc, path, "", ""))


def same_page(a, b):
    """True when two URLs point at the same page, ignoring a trailing slash and www."""
    a, b = norm(a), norm(b)
    if not a or not b:
        return False
    pa, pb = urlsplit(a), urlsplit(b)
    return (bare_host(pa.netloc) == bare_host(pb.netloc)
            and pa.path.rstrip("/") == pb.path.rstrip("/") and pa.scheme == pb.scheme)


# ---------------------------------------------------------------- time

def now_iso():
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()


def parse_ts(s):
    """ISO-8601 (with or without zone), or YYYY-MM-DD -> aware UTC datetime. None on failure."""
    if not s:
        return None
    s = str(s).strip().replace("Z", "+00:00")
    try:
        d = dt.datetime.fromisoformat(s)
    except ValueError:
        try:
            d = dt.datetime.strptime(s[:10], "%Y-%m-%d")
        except ValueError:
            return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=dt.timezone.utc)
    return d.astimezone(dt.timezone.utc)


def days_since(s):
    d = parse_ts(s)
    if not d:
        return None
    return (dt.datetime.now(dt.timezone.utc) - d).total_seconds() / 86400


def pacific_day():
    """Google API quotas reset at midnight Pacific time."""
    try:
        from zoneinfo import ZoneInfo
        return dt.datetime.now(ZoneInfo("America/Los_Angeles")).date().isoformat()
    except Exception:
        return (dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=8)).date().isoformat()


def utf8_stdout():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
