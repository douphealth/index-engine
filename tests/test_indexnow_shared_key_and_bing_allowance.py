"""A key hosted on several sites is found without listing every site, and a used-up Bing allowance is reported as that.

On 26 Nov 2026 borderwise.online had 375 pages waiting and tenderatlas.shop 1,072, while the log said "Bing daily URL
quota is 0 or unknown". The quota was known: Bing allows a new site 100 URLs a day and the morning run had used them.
Both sites (and nine more built from the same template) already served an IndexNow key file named after its key, which
the engine never looked for because it only knew /indexnow-key.txt and per-site entries in indexnow_keys.json."""
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from engine import config, indexnow, store, submit, util

SITE = "example.test"
BASE = "https://example.test"
SHARED = "0123456789abcdef0123456789abcdef"
OTHER = "fedcba9876543210fedcba9876543210"


def memory_db():
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.executescript(store.SCHEMA)
    for migration in store.MIGRATIONS:
        db.execute(migration)
    return db


def add_pages(db, n, site=SITE, base=BASE):
    for i in range(n):
        url = f"{base}/p{i:04d}/"
        db.execute("INSERT INTO urls(url, site, kind, lastmod, in_sitemap) VALUES(?,?,?,?,1)",
                   (url, site, "post", "2026-11-01T00:00:00+00:00"))
        db.execute("INSERT INTO audits(url, checked_at, status, hops, noindex, robots_google, robots_bing, words) "
                   "VALUES(?,?,200,0,0,1,1,500)", (url, util.now_iso()))
    db.commit()


class Resp:
    def __init__(self, status, text=""):
        self.status_code, self.text = status, text


class Web:
    """A fake web: {url: body}; anything else is a 404. Records what was asked."""
    def __init__(self, files):
        self.files, self.asked = files, []

    def __call__(self, url, timeout=None, **kw):
        self.asked.append(url)
        return Resp(200, self.files[url]) if url in self.files else Resp(404, "not found")


class FakeIndexNow:
    def __init__(self, code=200):
        self.code, self.calls = code, []

    def submit(self, host, key, loc, urls):
        self.calls.append((host, key, loc, list(urls)))
        return self.code, "" if self.code in (200, 202) else "key not valid"


class FakeBing:
    def __init__(self, daily=100, monthly=1000, fail=False):
        self.daily, self.monthly, self.fail, self.sent, self.quota_calls = daily, monthly, fail, [], 0

    def quota(self, site_url):
        self.quota_calls += 1
        if self.fail:
            raise RuntimeError("GetUrlSubmissionQuota HTTP 500")
        return self.daily, self.monthly

    def submit(self, site_url, urls):
        self.sent += urls
        self.daily -= len(urls)
        return 200, "{}"


class KeysFile(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="indexnow-keys-"))
        self.keys = self.tmp / "indexnow_keys.json"
        patcher = patch.object(config, "INDEXNOW_KEYS_FILE", self.keys)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.db = memory_db()
        self.logs = []
        for mod in (submit, indexnow):
            p = patch.object(mod, "log", lambda *a, **k: self.logs.append(" ".join(str(x) for x in a)))
            p.start()
            self.addCleanup(p.stop)

    def write(self, data):
        self.keys.write_text(json.dumps(data), encoding="utf-8")

    def detect(self, files, site=SITE):
        web = Web(files)
        with patch.object(indexnow, "get", web), patch.object(indexnow.time, "sleep", lambda s: None):
            return indexnow.detect_key(self.db, site), web


class SharedKey(KeysFile):
    def test_a_shared_key_is_found_on_a_site_that_serves_it(self):
        self.write({"*": {"key": SHARED}})
        found, web = self.detect({f"{BASE}/{SHARED}.txt": SHARED + "\n"})
        self.assertEqual(found, ("example.test", SHARED, f"{BASE}/{SHARED}.txt"))
        self.assertEqual(store.get_meta(self.db, SITE).get("indexnow"), "ok")

    def test_a_site_that_does_not_serve_it_never_uses_it(self):
        self.write({"*": {"key": SHARED}})
        found, web = self.detect({})
        self.assertIsNone(found)
        self.assertEqual(store.get_meta(self.db, SITE).get("indexnow"), "no key file found")
        self.assertEqual(web.asked.count(f"{BASE}/{SHARED}.txt"), 1, "one request per shared key, no retries on a 404")

    def test_a_page_served_in_place_of_the_file_is_not_a_key(self):
        """A single-page app answers 200 with its HTML shell for any path."""
        self.write({"*": {"key": SHARED}})
        found, _ = self.detect({f"{BASE}/{SHARED}.txt": "<!doctype html><html><body>app</body></html>"})
        self.assertIsNone(found)

    def test_a_file_holding_another_key_is_refused(self):
        self.write({"*": {"key": SHARED}})
        found, _ = self.detect({f"{BASE}/{SHARED}.txt": OTHER})
        self.assertIsNone(found)

    def test_several_shared_keys_and_bad_entries(self):
        self.write({"*": {"key": "short", "keys": [OTHER, 7, None, SHARED, OTHER]}})
        self.assertEqual(indexnow._configured("example.test"), ({}, [OTHER, SHARED]))
        found, web = self.detect({f"{BASE}/{SHARED}.txt": SHARED})
        self.assertEqual(found[1], SHARED)

    def test_the_sites_own_entry_and_its_own_file_win_over_a_shared_key(self):
        self.write({"example.test": {"key": OTHER}, "*": {"key": SHARED}})
        found, _ = self.detect({f"{BASE}/{OTHER}.txt": OTHER, f"{BASE}/{SHARED}.txt": SHARED})
        self.assertEqual(found[1], OTHER)
        self.write({"*": {"key": SHARED}})
        found, _ = self.detect({f"{BASE}/indexnow-key.txt": OTHER, f"{BASE}/{SHARED}.txt": SHARED})
        self.assertEqual(found[1], OTHER)

    def test_a_shared_key_equal_to_the_sites_own_is_asked_once(self):
        self.write({"example.test": {"key": SHARED}, "*": {"key": SHARED}})
        found, web = self.detect({})
        self.assertIsNone(found)
        self.assertEqual(web.asked.count(f"{BASE}/{SHARED}.txt"), 1)

    def test_a_broken_or_odd_file_is_ignored(self):
        self.keys.write_text("{not json", encoding="utf-8")
        self.assertEqual(indexnow._configured("example.test"), ({}, []))
        for odd in ([SHARED], {"*": SHARED}, {"*": [SHARED]}, {"example.test": SHARED, "*": {"keys": "abc"}}):
            self.write(odd)
            self.assertEqual(indexnow._configured("example.test"), ({}, []), odd)
        self.keys.unlink()
        self.assertEqual(indexnow._configured("example.test"), ({}, []))

    def test_the_key_never_reaches_the_log(self):
        self.write({"*": {"key": SHARED}})
        self.detect({f"{BASE}/{SHARED}.txt": SHARED})
        self.detect({})
        self.assertFalse([line for line in self.logs if SHARED in line])


class Submitting(KeysFile):
    def run_submit(self, files, bing=None, inow=None, **kw):
        inow = inow or FakeIndexNow()
        with patch.object(indexnow, "get", Web(files)), patch.object(indexnow.time, "sleep", lambda s: None):
            summary = submit.run(self.db, [SITE], bing_client=bing, bing_sites={SITE: BASE + "/"} if bing else None,
                                 indexnow_client=inow, **kw)
        return summary, inow

    def test_a_site_with_the_shared_key_sends_everything_in_one_call(self):
        add_pages(self.db, 375)
        self.write({"*": {"key": SHARED}})
        bing = FakeBing(daily=0, monthly=400)
        summary, inow = self.run_submit({f"{BASE}/{SHARED}.txt": SHARED}, bing=bing)
        self.assertEqual(summary[SITE], {"indexnow": 375})
        self.assertEqual(len(inow.calls), 1)
        self.assertEqual(len(inow.calls[0][3]), 375)
        self.assertEqual(bing.quota_calls, 0, "Bing's API is not asked anything when IndexNow carries the URLs")
        summary, inow2 = self.run_submit({f"{BASE}/{SHARED}.txt": SHARED}, bing=bing)
        self.assertEqual(inow2.calls, [], "nothing is sent twice")

    def test_indexnow_refusing_the_batch_falls_back_to_bing(self):
        add_pages(self.db, 30)
        self.write({"*": {"key": SHARED}})
        bing = FakeBing(daily=100)
        summary, inow = self.run_submit({f"{BASE}/{SHARED}.txt": SHARED}, bing=bing, inow=FakeIndexNow(403))
        self.assertEqual(summary[SITE], {"indexnow": 0, "bing": 30})
        self.assertEqual(len(bing.sent), 30)
        self.assertTrue([line for line in self.logs if "falling back to the Bing" in line])

    def test_an_accepted_batch_does_not_also_go_to_bing(self):
        add_pages(self.db, 30)
        self.write({"*": {"key": SHARED}})
        bing = FakeBing(daily=100)
        for code in (200, 202):
            self.db.execute("DELETE FROM submissions")
            summary, _ = self.run_submit({f"{BASE}/{SHARED}.txt": SHARED}, bing=bing, inow=FakeIndexNow(code))
            self.assertEqual(summary[SITE], {"indexnow": 30})
        self.assertEqual(bing.sent, [])

    def test_a_used_up_allowance_is_named_with_what_waits(self):
        add_pages(self.db, 475)
        bing = FakeBing(daily=100, monthly=500)
        summary, _ = self.run_submit({}, bing=bing)
        self.assertEqual(summary[SITE], {"bing": 100})
        note = [line for line in self.logs if "URLs wait" in line]
        self.assertEqual(len(note), 1, self.logs)
        self.assertIn("today's Bing allowance is used (100 sent today); 375 URLs wait, about 4 more days at this rate", note[0])
        self.assertIn("IndexNow key file removes the limit", note[0])
        self.logs.clear()
        summary, _ = self.run_submit({}, bing=bing)             # the second run of the day: allowance is 0, and known
        self.assertEqual(summary[SITE], {})
        note = [line for line in self.logs if "URLs wait" in line]
        self.assertIn("today's Bing allowance is used (100 sent today); 375 URLs wait", note[0])
        self.assertFalse([line for line in self.logs if "unknown" in line])

    def test_more_waiting_than_the_month_allows_says_so(self):
        add_pages(self.db, 1172)
        bing = FakeBing(daily=100, monthly=500)
        self.run_submit({}, bing=bing)
        bing.monthly = 400
        self.logs.clear()
        self.run_submit({}, bing=bing)
        note = [line for line in self.logs if "URLs wait" in line][0]
        self.assertIn("1072 URLs wait, more than the 400 left in this month's allowance", note)

    def test_no_allowance_at_all_and_an_unreadable_one_are_told_apart(self):
        add_pages(self.db, 12)
        self.run_submit({}, bing=FakeBing(daily=0, monthly=0))
        self.assertTrue([line for line in self.logs if "Bing gives this site no URL allowance today; 12 URLs wait" in line], self.logs)
        self.logs.clear()
        self.run_submit({}, bing=FakeBing(fail=True))
        self.assertTrue([line for line in self.logs if "Bing quota check failed" in line])
        self.assertTrue([line for line in self.logs if "12 URLs wait: Bing's allowance could not be read" in line], self.logs)

    def test_nothing_waiting_asks_bing_nothing_and_says_so(self):
        add_pages(self.db, 5)
        bing = FakeBing(daily=100)
        self.run_submit({}, bing=bing)
        self.logs.clear()
        calls = bing.quota_calls
        self.run_submit({}, bing=bing)
        self.assertEqual(bing.quota_calls, calls)
        self.assertTrue([line for line in self.logs if "Bing API - nothing new or changed to send" in line])
        self.assertFalse([line for line in self.logs if "URLs wait" in line or "quota" in line])

    def test_the_owners_own_limit_is_not_blamed_on_bing(self):
        add_pages(self.db, 40)
        bing = FakeBing(daily=100)
        summary, _ = self.run_submit({}, bing=bing, max_per_site=10)
        self.assertEqual(summary[SITE], {"bing": 10})
        self.assertFalse([line for line in self.logs if "URLs wait" in line])
        self.logs.clear()
        self.run_submit({}, bing=bing, dry_run=True)
        self.assertFalse([line for line in self.logs if "URLs wait" in line])

    def test_yesterdays_submissions_do_not_count_as_today(self):
        add_pages(self.db, 3)
        self.db.execute("INSERT INTO submissions(url, channel, submitted_at, lastmod, http_status) VALUES(?,?,?,?,200)",
                        (f"{BASE}/p0000/", "bing", "2020-01-01T10:00:00+00:00", "2026-11-01T00:00:00+00:00"))
        self.db.execute("INSERT INTO submissions(url, channel, submitted_at, lastmod, http_status) VALUES(?,?,?,?,400)",
                        (f"{BASE}/p0001/", "bing", util.now_iso(), "2026-11-01T00:00:00+00:00"))
        self.db.execute("INSERT INTO submissions(url, channel, submitted_at, lastmod, http_status) VALUES(?,?,?,?,200)",
                        (f"{BASE}/p0002/", "indexnow", util.now_iso(), "2026-11-01T00:00:00+00:00"))
        self.assertEqual(submit._bing_sent_today(self.db, SITE), 0)

    def test_the_note_wording(self):
        self.assertEqual(submit.bing_wait_note(1, 100, 0, 400),
                         "today's Bing allowance is used (100 sent today); 1 URLs wait, about 1 more day at this rate. "
                         "An IndexNow key file removes the limit (README: IndexNow setup)")


if __name__ == "__main__":
    unittest.main()
