# -*- coding: utf-8 -*-
"""WordPress REST client for the link autopilot. Logs in with an application password from the site's
credentials.txt (same files and layout the other portfolio scripts use). Credentials are never printed;
the working login is remembered by hash only (out/wp_auth_hint.json)."""
import base64
import hashlib
import json
import re
from urllib.parse import urlsplit

import requests

from . import config
from .util import SECRETS, log, norm, same_page

SITE_DIRS = {
    "gearuptofit.com": "01-gearuptofit", "affiliatemarketingforsuccess.com": "02-affiliatemarketingforsuccess",
    "mysticaldigits.com": "03-mysticaldigits", "frenchyfab.com": "04-frenchyfab", "micegoneguide.com": "05-micegoneguide",
    "gearuptogrow.com": "06-gearuptogrow", "plantastichaven.com": "07-plantastichaven",
    "efficientgptprompts.com": "08-efficientgptprompts",
}
KNOWN_USERS = {"frenchyfab.com": ["Alexios"], "mysticaldigits.com": ["Alexios"], "efficientgptprompts.com": ["Alexios"],
               "affiliatemarketingforsuccess.com": ["Papalexios"], "plantastichaven.com": ["Admin"]}
APP_PW = re.compile(r"\b[A-Za-z0-9]{4}(?: ?[A-Za-z0-9]{4}){5}\b")


def _hints():
    return config.OUT / "wp_auth_hint.json"


def _candidates(site):
    path = config.PORTFOLIO / SITE_DIRS.get(site, "-") / "credentials.txt"
    if not path.exists():
        return []
    txt = path.read_text(encoding="utf-8", errors="replace")
    pws = APP_PW.findall(txt) + [m.strip() for m in re.findall(r"(?im)password\s*[:=]\s*([^\r\n]+)", txt)]
    users = re.findall(r"(?im)^\s*(?:wp[-_ ]?)?user(?:name)?\s*[:=]\s*(\S+)", txt) + KNOWN_USERS.get(site, []) + \
        ["Alexios", "Papalexios", "Admin", "admin"]
    pws, users = list(dict.fromkeys(p for p in pws if p)), list(dict.fromkeys(users))
    SECRETS.extend(pws)
    return [(u, p) for p in pws[:6] for u in users[:8]]


class WPClient:
    def __init__(self, site, base=None, creds=None):
        self.site = site
        self.base = (base or config.site_base(site)).rstrip("/")
        self.s = requests.Session()
        self.s.headers.update({"User-Agent": config.UA})
        self.ok = self._login(creds if creds is not None else _candidates(site))

    def _login(self, combos):
        try:
            hints = json.loads(_hints().read_text(encoding="utf-8"))
        except (OSError, ValueError):
            hints = {}
        digest = lambda u, p: hashlib.sha256(f"{u}:{p}".encode()).hexdigest()  # noqa: E731
        for u, p in sorted(combos, key=lambda c: digest(*c) != hints.get(self.site)):
            auth = "Basic " + base64.b64encode(f"{u}:{p}".encode()).decode()
            try:
                r = self.s.get(f"{self.base}/wp-json/wp/v2/users/me", params={"context": "edit"},
                               headers={"Authorization": auth}, timeout=40)
            except requests.RequestException:
                continue
            if r.status_code == 200:
                self.s.headers["Authorization"] = auth
                if hints.get(self.site) != digest(u, p):
                    hints[self.site] = digest(u, p)
                    _hints().parent.mkdir(parents=True, exist_ok=True)
                    _hints().write_text(json.dumps(hints), encoding="utf-8")
                return True
        log(f"[wp] {self.site}: no working application-password login")
        return False

    def _call(self, method, path, **kw):
        r = self.s.request(method, f"{self.base}/wp-json/wp/v2/{path}", timeout=90, **kw)
        if r.status_code >= 300:
            raise RuntimeError(f"{method} {path} -> HTTP {r.status_code}: {r.text[:160]}")
        return r.json()

    def find(self, url):
        """(id, type) of the published post or page at this URL, or None."""
        slug = [s for s in urlsplit(url).path.split("/") if s]
        if not slug:
            return None
        for typ in ("posts", "pages"):
            for it in self._call("GET", typ, params={"slug": slug[-1], "_fields": "id,link,status"}):
                if same_page(norm(it.get("link", "")), url) and it.get("status") == "publish":
                    return it["id"], typ
        return None

    def raw(self, pid, typ):
        d = self._call("GET", f"{typ}/{pid}", params={"context": "edit", "_fields": "id,content,modified_gmt,link"})
        return d["content"]["raw"], d.get("modified_gmt")

    def corpus(self, after=None, per_page=50):
        """Every published post and page with its stored content, in pages of per_page (optionally only those
        modified after an ISO time). Yields {"url", "id", "type", "modified", "raw"}."""
        for typ in ("posts", "pages"):
            page, pages = 1, 1
            while page <= pages:
                params = {"context": "edit", "status": "publish", "per_page": per_page, "page": page, "orderby": "id",
                          "_fields": "id,link,modified_gmt,content"}
                if after:
                    params["modified_after"] = after
                r = self.s.get(f"{self.base}/wp-json/wp/v2/{typ}", params=params, timeout=180)
                if r.status_code == 400 and page > 1:
                    break                                   # past the last page
                if r.status_code >= 300:
                    raise RuntimeError(f"GET {typ} page {page} -> HTTP {r.status_code}: {r.text[:120]}")
                pages = int(r.headers.get("X-WP-TotalPages") or 1)
                for it in r.json():
                    raw = (it.get("content") or {}).get("raw")
                    if raw and it.get("link"):
                        yield {"url": norm(it["link"]), "id": it["id"], "type": typ, "modified": it.get("modified_gmt"),
                               "raw": raw}
                page += 1

    def update(self, pid, typ, raw):
        return self._call("POST", f"{typ}/{pid}", json={"content": raw})
