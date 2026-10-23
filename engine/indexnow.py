# -*- coding: utf-8 -*-
"""IndexNow: one POST notifies Bing, Yandex, Seznam, Naver and Yep (they share submissions).

A site needs a key file it hosts itself. This module finds an existing one (indexnow_keys.json,
a robots.txt comment, or /indexnow-key.txt) and verifies it is live before using it.
The key is never printed.
"""
import json
import re
import time
from urllib.parse import urlsplit

import requests

from . import config, store
from .util import SECRETS, get, log

KEY_RE = re.compile(r"^[A-Za-z0-9-]{8,128}$")


class IndexNowClient:
    def submit(self, host, key, key_location, urls):
        r = requests.post(config.INDEXNOW_ENDPOINT, json={"host": host, "key": key, "keyLocation": key_location, "urlList": urls},
                          headers={"Content-Type": "application/json; charset=utf-8", "User-Agent": config.UA}, timeout=120)
        return r.status_code, r.text[:200]


def _fetch(location, tries=3):
    """GET with retries: a CDN hiccup must not cost a whole day of IndexNow submissions."""
    last = None
    for i in range(tries):
        try:
            r = get(location, timeout=(10, 30))
            if r.status_code == 200 or r.status_code == 404:
                return r, None
            last = f"HTTP {r.status_code}"
        except Exception as e:
            last = type(e).__name__
        time.sleep(2 * (i + 1))
    return None, last


def _verify(location, expected=None):
    r, err = _fetch(location)
    if r is None:
        log(f"[indexnow] key file check failed ({err}) for {urlsplit(location).netloc}")
        return None
    if r.status_code != 200:
        return None
    key = re.sub(r"\s+", "", r.text)
    if not KEY_RE.match(key) or (expected and key != expected):
        return None
    stem = urlsplit(location).path.rsplit("/", 1)[-1][:-4]
    if KEY_RE.match(stem) and len(stem) >= 16 and stem != key:
        return None          # a <key>.txt file must contain that same key
    return key


def detect_key(db, site):
    meta = store.get_meta(db, site)
    base = meta.get("base") or config.site_base(site)
    host = urlsplit(base).netloc
    configured = {}
    if config.INDEXNOW_KEYS_FILE.exists():
        try:
            configured = json.loads(config.INDEXNOW_KEYS_FILE.read_text(encoding="utf-8")).get(host.replace("www.", ""), {})
        except ValueError:
            log("[indexnow] indexnow_keys.json is not valid JSON - ignored")
    candidates = []
    if configured.get("key"):
        candidates.append((configured.get("keyLocation") or f"{base}/{configured['key']}.txt", configured["key"]))
    for m in re.finditer(r"(?i)indexnow[^\n]*?(https?://[^\s#]+\.txt)", meta.get("robots") or ""):
        candidates.append((m.group(1), None))
    candidates += [(base + "/indexnow-key.txt", None), (base + "/indexnow.txt", None)]
    for loc, expected in candidates:
        if urlsplit(loc).netloc.lower() != host.lower():
            continue
        key = _verify(loc, expected)
        if key:
            SECRETS.append(key)
            store.set_meta(db, site, indexnow="ok")
            return host, key, loc
    store.set_meta(db, site, indexnow="no key file found")
    return None
