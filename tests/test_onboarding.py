"""A new site gets Google and Bing access with no clicks (engine/onboard.py).

Until 27 Nov 2026 every new site needed browser work in two accounts (the service account added as a Search Console
user, the site shared with the engine's Bing account); 11 sites took about an hour on 25 Nov. Neither step has an API.
Here the service account verifies the Domain property itself through a DNS TXT record, and the site is added to the
engine's Bing account and verified through that account's DNS CNAME."""
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from engine import config, onboard, store, util

CODE = "0123456789ABCDEF0123456789ABCDEF"
TXT = "google-site-verification=abcDEF123"


def memory_db():
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.executescript(store.SCHEMA)
    for migration in store.MIGRATIONS:
        try:
            db.execute(migration)
        except sqlite3.OperationalError:
            pass
    return db


class Resp:
    def __init__(self, status, body=None, text=None):
        self.status_code, self._body = status, body if body is not None else {}
        self.text = text if text is not None else json.dumps(self._body)

    def json(self):
        return self._body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class FakeCloudflare:
    """tokens: {token: {"zones": {name: id}, "write": bool, "read": bool}}; records: {zone id: [record]}."""

    def __init__(self, tokens):
        self.tokens, self.records, self.writes = tokens, {}, []

    def _tok(self, headers):
        return self.tokens.get(headers["Authorization"].split()[-1])

    def get(self, url, headers=None, params=None, timeout=None):
        t = self._tok(headers)
        if t is None:
            return Resp(401)
        if url.endswith("/zones"):
            return Resp(200, {"result": [{"name": n, "id": i} for n, i in t["zones"].items()],
                              "result_info": {"total_pages": 1}})
        zid = url.split("/zones/")[1].split("/")[0]
        if not t.get("read", True):
            return Resp(403)
        recs = [r for r in self.records.get(zid, []) if r["type"] == params["type"] and r["name"] == params["name"]]
        return Resp(200, {"result": recs})

    def post(self, url, headers=None, json=None, timeout=None):
        t = self._tok(headers)
        if not t or not t.get("write"):
            return Resp(403, text='{"success":false}')
        zid = url.split("/zones/")[1].split("/")[0]
        self.records.setdefault(zid, []).append(dict(json))
        self.writes.append((zid, dict(json)))
        return Resp(200, {"result": json})


class FakeGoogle:
    def __init__(self, visible_after=1, disabled=False):
        self.calls, self.visible_after, self.disabled = [], visible_after, disabled
        self.tries, self.owners = 0, ["sa@project.iam.gserviceaccount.com"]

    def token(self, domain):
        self.calls.append(("token", domain))
        if self.disabled:
            raise onboard.Missing("Google's Site Verification API is off in the service account's Google Cloud project")
        return TXT

    def verify(self, domain):
        self.calls.append(("verify", domain))
        self.tries += 1
        if self.tries < self.visible_after:
            raise onboard.Waiting("Google does not see the TXT record yet")
        return {"id": f"dns%3A%2F%2F{domain}", "site": {"type": "INET_DOMAIN", "identifier": domain}, "owners": list(self.owners)}

    def add_owners(self, resource, emails):
        self.calls.append(("owners", tuple(emails)))
        new = [e for e in emails if e not in self.owners]
        self.owners += new
        return new

    def add_property(self, prop):
        self.calls.append(("add", prop))


class FakeBing:
    def __init__(self, held=(), verifies=True):
        self.held, self.verifies, self.added, self.verified = list(held), verifies, [], []

    def user_sites(self):
        return [{"Url": f"https://{d}/", "IsVerified": True} for d in self.held]

    def add_site(self, url):
        self.added.append(url)

    def verify_site(self, url):
        self.verified.append(url)
        return self.verifies


class Base(unittest.TestCase):
    def setUp(self):
        self.db = memory_db()
        self.tmp = Path(tempfile.mkdtemp(prefix="onboard-"))
        for name, value in (("ONBOARDING_FILE", self.tmp / "onboarding.json"), ("DNS_TOKEN_FILES", []),
                            ("SITES", ["new.shop", "old.com"])):
            p = patch.object(config, name, value)
            p.start()
            self.addCleanup(p.stop)
        self.logs = []
        p = patch.object(onboard, "log", lambda *a, **k: self.logs.append(" ".join(map(str, a))))
        p.start()
        self.addCleanup(p.stop)
        self.cf = FakeCloudflare({"cfat_writer000000000000000000000000": {"zones": {"new.shop": "z1"}, "write": True}})
        self.dns = onboard.CloudflareDNS(list(self.cf.tokens), http=self.cf)

    def owners_file(self, **data):
        config.ONBOARDING_FILE.write_text(json.dumps(data), encoding="utf-8")

    def state(self, site, engine):
        return (store.get_meta(self.db, site).get("onboarding") or {}).get(engine) or {}


class Google(Base):
    def test_the_service_account_verifies_the_domain_and_adds_the_property(self):
        self.owners_file(google_owners=["owner@example.com"])
        g = FakeGoogle()
        got, _ = onboard.run(self.db, ["new.shop"], sa={"x": 1}, g=g, dns=self.dns)
        self.assertEqual(got, {"new.shop": "sc-domain:new.shop"})
        self.assertEqual(self.cf.writes, [("z1", {"type": "TXT", "name": "new.shop", "content": TXT, "ttl": 1})])
        self.assertEqual([c[0] for c in g.calls], ["token", "verify", "owners", "add"])
        self.assertIn(("owners", ("owner@example.com",)), g.calls)
        self.assertEqual(self.state("new.shop", "google")["state"], "done")

    def test_a_record_already_there_is_not_written_again(self):
        self.cf.records["z1"] = [{"type": "TXT", "name": "new.shop", "content": f'"{TXT}"'}]
        onboard.run(self.db, ["new.shop"], sa={"x": 1}, g=FakeGoogle(), dns=self.dns)
        self.assertEqual(self.cf.writes, [])

    def test_waiting_for_dns_is_finished_by_a_later_run_and_not_retried_too_soon(self):
        g = FakeGoogle(visible_after=2)
        got, _ = onboard.run(self.db, ["new.shop"], sa={"x": 1}, g=g, dns=self.dns)
        self.assertEqual(got, {})
        self.assertEqual(self.state("new.shop", "google")["state"], "waiting")
        self.assertTrue([x for x in self.logs if "The next run finishes it" in x])
        got, _ = onboard.run(self.db, ["new.shop"], sa={"x": 1}, g=g, dns=self.dns)
        self.assertEqual((got, g.tries), ({}, 1), "tried again only after ONBOARD_RETRY_HOURS")
        got, _ = onboard.run(self.db, ["new.shop"], sa={"x": 1}, g=g, dns=self.dns, force=True)
        self.assertEqual(got, {"new.shop": "sc-domain:new.shop"})
        self.assertEqual(len(self.cf.writes), 1, "the TXT record from the first run is reused")

    def test_a_site_already_reachable_or_done_is_left_alone(self):
        g = FakeGoogle()
        got, _ = onboard.run(self.db, ["new.shop"], sa={"x": 1}, props={"new.shop": "sc-domain:new.shop"}, g=g, dns=self.dns)
        self.assertEqual((got, g.calls), ({}, []))
        onboard._record(self.db, "new.shop", "google", "done", "sc-domain:new.shop")
        onboard.run(self.db, ["new.shop"], sa={"x": 1}, g=g, dns=self.dns, force=False)
        self.assertEqual(g.calls, [])

    def test_a_zone_no_token_reaches_is_named(self):
        onboard.run(self.db, ["other.org"], sa={"x": 1}, g=FakeGoogle(), dns=self.dns)
        st = self.state("other.org", "google")
        self.assertEqual(st["state"], "missing")
        self.assertIn("not in any Cloudflare account", st["note"])

    def test_a_token_that_cannot_write_dns_is_passed_over(self):
        cf = FakeCloudflare({"cfat_reader000000000000000000000000": {"zones": {"new.shop": "z1"}, "write": False},
                             "cfut_writer000000000000000000000000": {"zones": {"new.shop": "z1"}, "write": True}})
        dns = onboard.CloudflareDNS(list(cf.tokens), http=cf)
        got, _ = onboard.run(self.db, ["new.shop"], sa={"x": 1}, g=FakeGoogle(), dns=dns)
        self.assertEqual(got, {"new.shop": "sc-domain:new.shop"})
        cf2 = FakeCloudflare({"cfat_reader000000000000000000000000": {"zones": {"new.shop": "z1"}, "write": False, "read": False}})
        dns2 = onboard.CloudflareDNS(list(cf2.tokens), http=cf2)
        onboard.run(self.db, ["new.shop"], sa={"x": 1}, g=FakeGoogle(), dns=dns2, force=True)
        self.assertIn("no Cloudflare token may edit DNS in the new.shop zone", self.state("new.shop", "google")["note"])

    def test_a_subdomain_site_uses_its_parent_zone(self):
        name, access = self.dns.zone_of("blog.new.shop")
        self.assertEqual((name, len(access)), ("new.shop", 1))

    def test_the_api_switched_off_is_reported_with_its_link(self):
        r = Resp(403, text='{"error":{"code":403,"message":"Site Verification API has not been used in project 5 before or '
                           'it is disabled. Enable it by visiting https://console.developers.google.com/apis/api/'
                           'siteverification.googleapis.com/overview?project=5 then retry."}}')
        with self.assertRaises(onboard.Missing) as e:
            onboard.GoogleOnboarding._explain(r, "x")
        self.assertIn("Site Verification API is off", str(e.exception))
        self.assertIn("overview?project=5", str(e.exception))
        with self.assertRaises(RuntimeError):
            onboard.GoogleOnboarding._explain(Resp(500, text="boom"), "x")

    def test_a_dry_run_writes_nothing_and_records_nothing(self):
        g = FakeGoogle()
        got, _ = onboard.run(self.db, ["new.shop"], sa={"x": 1}, g=g, dns=self.dns, dry_run=True)
        self.assertEqual(got, {"new.shop": "sc-domain:new.shop"})
        self.assertEqual(self.cf.writes, [])
        self.assertEqual([c[0] for c in g.calls], ["token"])
        self.assertEqual(self.state("new.shop", "google"), {})
        self.assertTrue([x for x in self.logs if "would set up" in x])


class GoogleClient(Base):
    """The real client's HTTP calls, against a fake session."""

    class Session:
        def __init__(self, answers):
            self.answers, self.calls = answers, []

        def _answer(self, method, url, **kw):
            self.calls.append((method, url, kw.get("json"), kw.get("params")))
            for key, resp in self.answers:
                if key in url and (method, key) not in [(c[0], k) for c in self.calls[:-1] for k, _ in [(key, None)] if False]:
                    return resp
            return Resp(404)

        def post(self, url, **kw):
            return self._answer("POST", url, **kw)

        def put(self, url, **kw):
            return self._answer("PUT", url, **kw)

    def test_the_requests_google_receives(self):
        s = self.Session([("/token", Resp(200, {"method": "DNS_TXT", "token": TXT})),
                          ("/webResource/", Resp(200, {})),
                          ("/webResource", Resp(200, {"id": "dns%3A%2F%2Fnew.shop", "site": {"type": "INET_DOMAIN",
                                                      "identifier": "new.shop"}, "owners": ["sa@p.iam.gserviceaccount.com"]})),
                          ("/webmasters/v3/sites/", Resp(204))])
        g = onboard.GoogleOnboarding({"x": 1}, session=s)
        self.assertEqual(g.token("new.shop"), TXT)
        res = g.verify("new.shop")
        self.assertEqual(g.add_owners(res, ["Owner@Example.com", "sa@p.iam.gserviceaccount.com"]), ["Owner@Example.com"])
        g.add_property("sc-domain:new.shop")
        methods = [(m, u.split("googleapis.com")[1]) for m, u, _, _ in s.calls]
        self.assertEqual(methods, [("POST", "/siteVerification/v1/token"), ("POST", "/siteVerification/v1/webResource"),
                                   ("PUT", "/siteVerification/v1/webResource/dns%3A%2F%2Fnew.shop"),
                                   ("PUT", "/webmasters/v3/sites/sc-domain%3Anew.shop")])
        self.assertEqual(s.calls[0][2], {"site": {"type": "INET_DOMAIN", "identifier": "new.shop"}, "verificationMethod": "DNS_TXT"})
        self.assertEqual(s.calls[1][3], {"verificationMethod": "DNS_TXT"})
        self.assertEqual(s.calls[2][2]["owners"], ["sa@p.iam.gserviceaccount.com", "Owner@Example.com"])

    def test_google_not_seeing_the_record_yet_is_waiting(self):
        s = self.Session([("/webResource", Resp(400, text="The necessary verification token could not be found"))])
        with self.assertRaises(onboard.Waiting):
            onboard.GoogleOnboarding({"x": 1}, session=s).verify("new.shop")

    def test_no_owner_update_when_nobody_is_new(self):
        s = self.Session([])
        g = onboard.GoogleOnboarding({"x": 1}, session=s)
        self.assertEqual(g.add_owners({"id": "x", "site": {}, "owners": ["a@b.c"]}, ["A@B.C"]), [])
        self.assertEqual(s.calls, [])


class Bing(Base):
    def setUp(self):
        super().setUp()
        self.home = {}
        p = patch.object(onboard, "get", lambda url, timeout=None: Resp(200, text=self.home.get(url, "<html></html>")))
        p.start()
        self.addCleanup(p.stop)

    def test_the_site_is_added_and_verified_with_the_accounts_cname(self):
        self.home["https://old.com/"] = f'<head><meta name="msvalidate.01" content="{CODE}" /></head>'
        b = FakeBing(held=["old.com"])
        _, got = onboard.run(self.db, ["new.shop"], bclient=b, dns=self.dns)
        self.assertEqual(got, {"new.shop": "https://new.shop/"})
        self.assertEqual(self.cf.writes, [("z1", {"type": "CNAME", "name": f"{CODE.lower()}.new.shop", "content": "verify.bing.com",
                                                  "ttl": 1, "proxied": False})])
        self.assertEqual((b.added, b.verified), (["https://new.shop/"], ["https://new.shop/"]))
        self.assertEqual(store.get_meta(self.db, "__engine__")["bing_code"], CODE, "the code is remembered")
        self.assertEqual(self.state("new.shop", "bing")["state"], "done")

    def test_bing_not_seeing_the_record_yet_is_waiting(self):
        self.owners_file(bing_code=CODE.lower())
        b = FakeBing(verifies=False)
        _, got = onboard.run(self.db, ["new.shop"], bclient=b, dns=self.dns)
        self.assertEqual(got, {})
        self.assertEqual(self.state("new.shop", "bing")["state"], "waiting")

    def test_no_code_or_disagreeing_codes_are_never_guessed(self):
        b = FakeBing(held=["old.com", "new.shop"])
        self.assertIsNone(onboard.bing_code(self.db, b, ["old.com", "new.shop"]))
        self.home["https://old.com/"] = f'<meta content="{CODE}" name="msvalidate.01">'
        self.home["https://new.shop/"] = '<meta name="msvalidate.01" content="FFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFF">'
        self.assertIsNone(onboard.bing_code(self.db, b, ["old.com", "new.shop"]))
        onboard.run(self.db, ["other.shop"], bclient=b, dns=self.dns)
        self.assertIn("Bing account code is unknown", self.state("other.shop", "bing")["note"])
        self.assertEqual(b.added, [])

    def test_a_site_held_but_unverified_is_verified(self):
        self.owners_file(bing_code=CODE)
        store.set_meta(self.db, "new.shop", bing_verified=False)
        b = FakeBing()
        _, got = onboard.run(self.db, ["new.shop"], bclient=b, bsites={"new.shop": "https://new.shop/"}, dns=self.dns)
        self.assertEqual(got, {"new.shop": "https://new.shop/"})

    def test_a_site_already_in_bing_is_left_alone(self):
        b = FakeBing()
        _, got = onboard.run(self.db, ["new.shop"], bclient=b, bsites={"new.shop": "https://new.shop/"}, dns=self.dns)
        self.assertEqual((got, b.added), ({}, []))


class Tokens(Base):
    def test_tokens_are_read_from_every_file_and_kept_secret(self):
        a = self.tmp / "a.txt"
        a.write_text("acct: 0123456789abcdef0123456789abcdef\nCF API TOKEN:\tcfat_AAAAAAAAAAAAAAAAAAAAAAAAAAAA\n"
                     "CF API TOKEN:\tcfut_BBBBBBBBBBBBBBBBBBBBBBBBBBBB\n{\"x\": \"cfat_AAAAAAAAAAAAAAAAAAAAAAAAAAAA\"}\n", encoding="utf-8")
        legacy = self.tmp / "legacy.txt"
        legacy.write_text("token: " + "L" * 40 + "\n", encoding="utf-8")
        with patch.object(config, "DNS_TOKEN_FILES", [a, self.tmp / "missing.txt", legacy]), \
                patch.object(config, "CF_TOKEN_FILE", legacy):
            toks = onboard.dns_tokens()
        self.assertEqual(toks, ["cfat_AAAAAAAAAAAAAAAAAAAAAAAAAAAA", "cfut_BBBBBBBBBBBBBBBBBBBBBBBBBBBB", "L" * 40])
        for t in toks:
            self.assertIn(t, util.SECRETS)

    def test_broken_settings_file_is_ignored(self):
        config.ONBOARDING_FILE.write_text("{not json", encoding="utf-8")
        self.assertEqual(onboard.settings(), {})
        config.ONBOARDING_FILE.write_text("[1]", encoding="utf-8")
        self.assertEqual(onboard.settings(), {})


class Wiring(unittest.TestCase):
    def test_measure_only_runs_never_onboard(self):
        import indexengine
        args = indexengine.build_parser().parse_args(["run", "--no-submit"])
        with patch.object(indexengine, "load_credentials", return_value=({"x": 1}, ["k"])), \
                patch.object(indexengine.google, "GSCClient"), patch.object(indexengine.bing, "working_client"), \
                patch.object(indexengine.google, "resolve_properties", return_value={}), \
                patch.object(indexengine.bing, "resolve_sites", return_value={}), \
                patch.object(onboard, "run") as ob:
            indexengine.engines(args, memory_db(), ["new.shop"])
            ob.assert_not_called()
            indexengine.engines(indexengine.build_parser().parse_args(["run", "--no-onboard"]), memory_db(), ["new.shop"])
            ob.assert_not_called()

    def test_a_normal_run_onboards_then_resolves_only_the_new_sites(self):
        import indexengine
        args = indexengine.build_parser().parse_args(["run"])
        with patch.object(indexengine, "load_credentials", return_value=({"x": 1}, ["k"])), \
                patch.object(indexengine.google, "GSCClient"), patch.object(indexengine.bing, "working_client"), \
                patch.object(indexengine.google, "resolve_properties",
                             side_effect=[{"old.com": "sc-domain:old.com"}, {"new.shop": "sc-domain:new.shop"}]) as rp, \
                patch.object(indexengine.bing, "resolve_sites", return_value={}), \
                patch.object(onboard, "run", return_value=({"new.shop": "sc-domain:new.shop"}, {})):
            _, _, props, _ = indexengine.engines(args, memory_db(), ["old.com", "new.shop"])
        self.assertEqual(props, {"old.com": "sc-domain:old.com", "new.shop": "sc-domain:new.shop"})
        self.assertEqual(rp.call_args_list[1].args[2], ["new.shop"])


if __name__ == "__main__":
    unittest.main()
