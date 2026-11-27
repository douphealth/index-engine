# -*- coding: utf-8 -*-
"""Automatic onboarding: a new site gets Google and Bing access with no clicks.

Until 27 Nov 2026 every new site needed browser work in two accounts: adding the service account as a user of the
Search Console property, and sharing the site with the engine's Bing account. Neither has an API. This module removes
both steps where the site's DNS is on Cloudflare:

Google  the service account asks the Site Verification API for its DNS token, writes it as a TXT record at the
        domain, becomes a verified owner of the Domain property, and adds sc-domain:<domain> to its Search Console
        list. Owners listed in onboarding.json ("google_owners") are added too, so the property shows in their
        Search Console.
Bing    the site is added to the Bing account the engine's key belongs to (AddSite) and verified with that account's
        DNS record: a CNAME named after the account's code, pointing at verify.bing.com. The code is the account's
        msvalidate.01 code, read from a site the account already holds that carries it (or onboarding.json "bing_code").

Needs a Cloudflare token with DNS Write on the zone (any token in the files listed in config.DNS_TOKEN_FILES), and the
Site Verification API enabled in the service account's Google Cloud project. Nothing is printed: tokens go to SECRETS.
Every step checks before it writes, so a run that stops half-way is finished by a later one; a site is tried at most
every ONBOARD_RETRY_HOURS. What is missing is said in one line per site and kept in site_meta["onboarding"].
"""
import json
import re
from collections import Counter
from urllib.parse import quote

import requests

from . import config, store
from .util import SECRETS, days_since, get, log, now_iso

CF_API = "https://api.cloudflare.com/client/v4"
SV_API = "https://www.googleapis.com/siteVerification/v1"
WM_API = "https://www.googleapis.com/webmasters/v3"
BING_TARGET = "verify.bing.com"
TOKEN_RE = re.compile(r"\bcf[a-z]t_[A-Za-z0-9_-]{20,}")
LEGACY_TOKEN_RE = re.compile(r"(?<![A-Za-z0-9_-])[A-Za-z0-9_-]{40}(?![A-Za-z0-9_-])")
BING_CODE_RE = re.compile(r"^[0-9A-F]{32}$")


class Missing(Exception):
    """Something the owner has to provide once (a DNS token for the zone, an API to switch on). Retried later."""


class Waiting(Exception):
    """A step is done but not visible yet (DNS propagation). Retried on the next run."""


# ---------------------------------------------------------------- local settings and tokens

def settings():
    """onboarding.json next to indexengine.py (not in git): {"google_owners": [emails], "bing_code": "..."}."""
    try:
        data = json.loads(config.ONBOARDING_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def dns_tokens():
    """Every Cloudflare token in the configured files, in file order. Added to SECRETS, never printed."""
    out = []
    for path in config.DNS_TOKEN_FILES:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        found = TOKEN_RE.findall(text)
        if not found and path == config.CF_TOKEN_FILE and text.split():
            found = [t for t in LEGACY_TOKEN_RE.findall(text)][-1:]
        out += found
    out = list(dict.fromkeys(out))
    SECRETS.extend(out)
    return out


# ---------------------------------------------------------------- Cloudflare DNS

class CloudflareDNS:
    """Finds the zone of a domain among the tokens' zones, and writes TXT and CNAME records idempotently."""

    def __init__(self, tokens, http=None):
        self.tokens, self.http = list(tokens), http or requests
        self._zones = None

    def _h(self, tok):
        return {"Authorization": f"Bearer {tok}", "Content-Type": "application/json"}

    def zones(self):
        """{zone name: [(token, zone id), ...]} over every token (one zone may be reachable through several)."""
        if self._zones is None:
            self._zones = {}
            for tok in self.tokens:
                page = 1
                while True:
                    try:
                        r = self.http.get(f"{CF_API}/zones", headers=self._h(tok), params={"per_page": 50, "page": page},
                                          timeout=30)
                    except requests.RequestException:
                        break
                    if r.status_code != 200:
                        break
                    body = r.json()
                    for z in body.get("result") or []:
                        self._zones.setdefault(z["name"], []).append((tok, z["id"]))
                    info = body.get("result_info") or {}
                    if page >= (info.get("total_pages") or 1):
                        break
                    page += 1
        return self._zones

    def zone_of(self, domain):
        """(zone name, [(token, zone id)]) for the closest zone that holds the domain, or (None, [])."""
        parts = domain.lower().strip(".").split(".")
        for i in range(len(parts) - 1):
            name = ".".join(parts[i:])
            if name in self.zones():
                return name, self.zones()[name]
        return None, []

    def ensure(self, domain, rtype, name, content):
        """Make sure a record exists; returns "exists" or "created". Raises Missing when no token may write it."""
        zone, access = self.zone_of(domain)
        if not zone:
            raise Missing("its DNS zone is not in any Cloudflare account the engine has a token for")
        refused = []
        for tok, zid in access:
            try:
                r = self.http.get(f"{CF_API}/zones/{zid}/dns_records", headers=self._h(tok),
                                  params={"type": rtype, "name": name, "per_page": 100}, timeout=30)
                if r.status_code in (401, 403):
                    refused.append("read")
                    continue
                r.raise_for_status()
                want = content.strip('"').lower().rstrip(".")
                if any((x.get("content") or "").strip('"').lower().rstrip(".") == want for x in r.json().get("result") or []):
                    return "exists"
                body = {"type": rtype, "name": name, "content": content, "ttl": 1}
                if rtype == "CNAME":
                    body["proxied"] = False             # Bing must see the record itself, not Cloudflare's proxy
                w = self.http.post(f"{CF_API}/zones/{zid}/dns_records", headers=self._h(tok), json=body, timeout=30)
                if w.status_code in (401, 403):
                    refused.append("write")
                    continue
                if w.status_code not in (200, 201):
                    raise RuntimeError(f"Cloudflare HTTP {w.status_code}: {w.text[:160]}")
                return "created"
            except requests.RequestException as e:
                raise Waiting(f"Cloudflare did not answer ({type(e).__name__})")
        raise Missing(f"no Cloudflare token may edit DNS in the {zone} zone (needs Zone Read + DNS Write)")


# ---------------------------------------------------------------- Google

class GoogleOnboarding:
    """Site Verification + Search Console calls made as the service account."""

    def __init__(self, sa, session=None):
        self.sa, self._s = sa, session

    def s(self):
        if self._s is None:
            from google.auth.transport.requests import AuthorizedSession
            from google.oauth2 import service_account
            creds = service_account.Credentials.from_service_account_info(
                self.sa, scopes=["https://www.googleapis.com/auth/siteverification",
                                 "https://www.googleapis.com/auth/webmasters"])
            self._s = AuthorizedSession(creds)
        return self._s

    @staticmethod
    def _explain(r, what):
        text = r.text or ""
        if r.status_code == 403 and ("has not been used" in text or "is disabled" in text or "SERVICE_DISABLED" in text):
            m = re.search(r"https://console\.developers\.google\.com/apis/api/siteverification\.googleapis\.com/overview\?project=\d+", text)
            raise Missing("Google's Site Verification API is off in the service account's Google Cloud project"
                          + (f" (turn it on: {m.group(0)})" if m else ""))
        raise RuntimeError(f"{what} HTTP {r.status_code}: {text[:200]}")

    def token(self, domain):
        r = self.s().post(f"{SV_API}/token", json={"site": {"type": "INET_DOMAIN", "identifier": domain},
                                                   "verificationMethod": "DNS_TXT"}, timeout=60)
        if r.status_code != 200:
            self._explain(r, "siteVerification.getToken")
        tok = r.json().get("token") or ""
        if not tok.startswith("google-site-verification="):
            raise RuntimeError("siteVerification.getToken returned no DNS token")
        return tok

    def verify(self, domain):
        r = self.s().post(f"{SV_API}/webResource", params={"verificationMethod": "DNS_TXT"},
                          json={"site": {"type": "INET_DOMAIN", "identifier": domain}}, timeout=60)
        if r.status_code == 200:
            return r.json()
        if r.status_code == 400:
            raise Waiting("Google does not see the TXT record yet")
        self._explain(r, "siteVerification.insert")

    def add_owners(self, resource, emails):
        """Adds the owner's Google accounts as owners (delegated by the service account). Returns the ones added."""
        have = {o.lower() for o in resource.get("owners") or []}
        new = [e for e in emails if e.lower() not in have]
        if not new:
            return []
        body = {"site": resource["site"], "owners": list(resource.get("owners") or []) + new}
        r = self.s().put(f"{SV_API}/webResource/{quote(resource['id'], safe='%')}", json=body, timeout=60)
        if r.status_code != 200:
            raise RuntimeError(f"siteVerification.update HTTP {r.status_code}: {r.text[:160]}")
        return new

    def add_property(self, prop):
        r = self.s().put(f"{WM_API}/sites/{quote(prop, safe='')}", timeout=60)
        if r.status_code not in (200, 204):
            raise RuntimeError(f"sites.add HTTP {r.status_code}: {r.text[:160]}")


def onboard_google(domain, g, dns, owners=(), dry_run=False):
    """Returns the Search Console property it set up. Raises Missing / Waiting / RuntimeError."""
    prop = f"sc-domain:{domain}"
    if dry_run:
        zone, access = dns.zone_of(domain)
        if not zone:
            raise Missing("its DNS zone is not in any Cloudflare account the engine has a token for")
        g.token(domain)                                   # proves the API is on and the account may verify
        return prop
    record = g.token(domain)
    dns.ensure(domain, "TXT", domain, record)
    resource = g.verify(domain)
    added = g.add_owners(resource, owners) if owners else []
    g.add_property(prop)
    if added:
        log(f"[onboard] {domain}: {len(added)} owner account(s) added to the Search Console property")
    return prop


# ---------------------------------------------------------------- Bing

def bing_code(db, bclient, sites):
    """The engine's Bing account code (msvalidate.01), from onboarding.json or from sites that account holds.
    Kept in site_meta('__engine__') once found; a code is accepted only when the sites that carry one agree."""
    own = (settings().get("bing_code") or "").strip().upper()
    if BING_CODE_RE.match(own):
        return own
    meta = store.get_meta(db, "__engine__")
    if BING_CODE_RE.match(meta.get("bing_code") or ""):
        return meta["bing_code"]
    try:
        held = {e.get("Url", "").split("://")[-1].strip("/").lower().replace("www.", "") for e in bclient.user_sites()
                if e.get("IsVerified")}
    except Exception:
        return None
    found = Counter()
    for site in sites:
        if site.lower() not in held:
            continue
        try:
            r = get(config.site_base(site) + "/", timeout=(10, 20))
        except Exception:
            continue
        for m in re.finditer(r"<meta[^>]+>", r.text or "", re.I):
            tag = m.group(0)
            if re.search(r"name=[\"']msvalidate\.01[\"']", tag, re.I):
                c = re.search(r"content=[\"']([0-9A-Fa-f]{32})[\"']", tag)
                if c:
                    found[c.group(1).upper()] += 1
    if len(found) != 1:
        return None                                       # none, or sites disagree: do not guess
    code = next(iter(found))
    store.set_meta(db, "__engine__", bing_code=code)
    return code


def onboard_bing(domain, bclient, dns, code, dry_run=False):
    """Returns the Bing site URL once verified. Raises Missing / Waiting / RuntimeError."""
    url = f"https://{domain}/"
    if not code:
        raise Missing("the engine's Bing account code is unknown: no site it holds carries an msvalidate.01 tag "
                      "(put it in onboarding.json as \"bing_code\")")
    zone, _ = dns.zone_of(domain)
    if not zone:
        raise Missing("its DNS zone is not in any Cloudflare account the engine has a token for")
    if dry_run:
        return url
    dns.ensure(domain, "CNAME", f"{code.lower()}.{domain}", BING_TARGET)
    bclient.add_site(url)
    if not bclient.verify_site(url):
        raise Waiting("Bing does not see the CNAME record yet")
    return url


# ---------------------------------------------------------------- orchestration

def _due(db, site, engine_name):
    st = (store.get_meta(db, site).get("onboarding") or {}).get(engine_name) or {}
    if st.get("state") == "done":
        return False
    age = days_since(st.get("at"))
    return age is None or age * 24 >= config.ONBOARD_RETRY_HOURS


def _record(db, site, engine_name, state, note):
    meta = store.get_meta(db, site).get("onboarding") or {}
    meta[engine_name] = {"state": state, "note": note, "at": now_iso()}
    store.set_meta(db, site, onboarding=meta)


def _domain(site):
    return site.split("://")[-1].strip("/").lower()


def run(db, sites, sa=None, bclient=None, props=None, bsites=None, dry_run=False, force=False, g=None, dns=None):
    """Onboard every site the engine has no Google property or no verified Bing site for.
    Returns (google {site: property}, bing {site: url}) for the sites that are now ready."""
    props, bsites = props or {}, bsites or {}
    want_g = [s for s in sites if s not in props and sa and (force or _due(db, s, "google"))]
    want_b = [s for s in sites if bclient and (s not in bsites or store.get_meta(db, s).get("bing_verified") is False)
              and (force or _due(db, s, "bing"))]
    if not want_g and not want_b:
        return {}, {}
    dns = dns or CloudflareDNS(dns_tokens())
    g = g or (GoogleOnboarding(sa) if want_g else None)
    owners = [e for e in settings().get("google_owners") or [] if isinstance(e, str) and "@" in e]
    code = bing_code(db, bclient, config.SITES) if want_b else None
    done_g, done_b = {}, {}
    for engine_name, todo in (("google", want_g), ("bing", want_b)):
        for site in todo:
            domain = _domain(site)
            try:
                if engine_name == "google":
                    out = onboard_google(domain, g, dns, owners, dry_run)
                    done_g[site] = out
                else:
                    out = onboard_bing(domain, bclient, dns, code, dry_run)
                    done_b[site] = out
                verb = "would set up" if dry_run else "set up"
                log(f"[onboard] {site}: {engine_name.capitalize()} {verb}: {out}")
                if not dry_run:
                    _record(db, site, engine_name, "done", out)
            except Missing as e:
                log(f"[onboard] {site}: {engine_name.capitalize()} needs a one-time step: {e}")
                if not dry_run:
                    _record(db, site, engine_name, "missing", str(e))
            except Waiting as e:
                log(f"[onboard] {site}: {engine_name.capitalize()} started; {e}. The next run finishes it.")
                if not dry_run:
                    _record(db, site, engine_name, "waiting", str(e))
            except Exception as e:
                log(f"[onboard] {site}: {engine_name.capitalize()} failed: {type(e).__name__}: {str(e)[:200]}")
                if not dry_run:
                    _record(db, site, engine_name, "failed", f"{type(e).__name__}: {str(e)[:200]}")
    return done_g, done_b
