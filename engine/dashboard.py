# -*- coding: utf-8 -*-
"""Sync with the online dashboard (Cloudflare Pages): pull the owner's decisions and queued runs, push results.

Connection details live OUTSIDE the repo, in D:\\Claude\\SEO-Setup\\Index-Engine-Dashboard.json:
    {"url": "https://<project>.pages.dev", "token": "<ENGINE_TOKEN>"}
or in the environment (INDEX_ENGINE_DASHBOARD_URL / INDEX_ENGINE_DASHBOARD_TOKEN). The token is never printed.
A dashboard failure never stops a run: every call here is best effort and logged.
"""
import json
import os
import socket
from urllib.parse import quote

import requests

from . import config
from .util import SECRETS, log


class Dashboard:
    def __init__(self, url, token):
        self.url = url.rstrip("/")
        self.token = token
        SECRETS.append(token)
        self.s = requests.Session()
        self.s.headers.update({"Authorization": f"Bearer {token}", "X-Engine-Host": socket.gethostname()[:60],
                               "User-Agent": config.UA})

    @classmethod
    def from_config(cls):
        url, token = os.environ.get("INDEX_ENGINE_DASHBOARD_URL"), os.environ.get("INDEX_ENGINE_DASHBOARD_TOKEN")
        if not (url and token) and config.DASHBOARD_FILE.exists():
            try:
                d = json.loads(config.DASHBOARD_FILE.read_text(encoding="utf-8"))
                url, token = d.get("url"), d.get("token")
            except ValueError:
                log("[dashboard] connection file is not valid JSON - working offline")
        return cls(url, token) if url and token else None

    def _call(self, method, path, **kw):
        r = self.s.request(method, self.url + path, timeout=kw.pop("timeout", 60), **kw)
        if r.status_code >= 300:
            raise RuntimeError(f"{method} {path} -> HTTP {r.status_code}: {r.text[:160]}")
        return r.json() if "json" in r.headers.get("Content-Type", "") else r.text

    def state(self):
        return self._call("GET", "/api/engine/state")

    def command(self, cid, status, result=""):
        return self._call("POST", "/api/engine/command", json={"id": cid, "status": status, "result": result})

    def run_start(self, trigger, options):
        return self._call("POST", "/api/engine/run", json={"event": "start", "trigger": trigger, "options": options})["id"]

    def run_finish(self, run_id, status, summary=None, error=None):
        return self._call("POST", "/api/engine/run", json={"event": "finish", "id": run_id, "status": status,
                                                           "summary": summary, "error": error})

    def put(self, key, body):
        data = body if isinstance(body, (bytes, bytearray)) else (body if isinstance(body, str) else json.dumps(body, separators=(",", ":")))
        if isinstance(data, str):
            data = data.encode("utf-8")
        return self._call("PUT", f"/api/engine/ingest/{quote(key, safe=':')}", data=data,
                          headers={"Content-Type": "application/octet-stream"}, timeout=180)


def apply_state(db, state):
    """Store the owner's URL decisions locally (triage and submission read them) and return the settings."""
    rows = state.get("url_state") or []
    db.execute("DELETE FROM owner_state")
    db.executemany("INSERT OR REPLACE INTO owner_state(url, site, ignored, requested_at) VALUES(?,?,?,?)",
                   [(r["url"], r["site"], int(r.get("ignored") or 0), r.get("requested_at")) for r in rows])
    db.commit()
    settings = state.get("settings") or {}
    ignored = sum(1 for r in rows if r.get("ignored"))
    done = sum(1 for r in rows if r.get("requested_at"))
    log(f"[dashboard] pulled: {ignored} ignored URLs, {done} request-indexing marks, settings {sorted(k for k in settings if k != 'engine_last_seen')}")
    return settings


def safe(fn, *a, what="dashboard call", **kw):
    try:
        return fn(*a, **kw)
    except Exception as e:
        log(f"[dashboard] {what} failed: {str(e)[:200]}")
        return None
