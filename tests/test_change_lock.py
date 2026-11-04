import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _mock_github  # noqa: E402
from engine import axiom_cc as cc, changelock, links, util  # noqa: E402

TOKEN = "cc-test-token-XYZ123"


class ChangeLockTests(unittest.TestCase):
    def setUp(self):
        self.srv, self.st, url = _mock_github.start()
        self.c = cc.Client(repo="o/r", issue=1, token=TOKEN, api=url)
        self.env = mock.patch.dict(os.environ, {}, clear=False)
        self.env.start()
        for k in ("AXIOM_CC_MODE", "AXIOM_CC_FAIL_OPEN", "AXIOM_CC_TOKEN", "GITHUB_TOKEN"):
            os.environ.pop(k, None)
        changelock._settings_mode[0] = ""
        mock.patch.object(changelock, "client", lambda: self.c).start()
        self.logs = []
        mock.patch.object(changelock, "log", self.logs.append).start()

    def tearDown(self):
        mock.patch.stopall()
        self.env.stop()
        self.srv.shutdown()
        self.srv.server_close()
        changelock._held.clear()

    def refs(self):
        return sorted(r.split("axiom-cc/")[1] for r in self.st.refs)

    def test_default_is_observe_and_stricter_mode_wins(self):
        self.assertEqual(changelock.mode({}), "observe")
        os.environ["AXIOM_CC_MODE"] = "off"
        self.assertEqual(changelock.mode({}), "off")
        self.assertEqual(changelock.mode({"change_lock_mode": "enforce"}), "enforce")
        os.environ["AXIOM_CC_MODE"] = "enforce"
        self.assertEqual(changelock.mode({"change_lock_mode": "off"}), "enforce")

    def test_enforce_free_site_takes_and_releases_lock(self):
        os.environ["AXIOM_CC_MODE"] = "enforce"
        with changelock.site_write("https://www.test.example/", "t", "1 url") as ok:
            self.assertTrue(ok)
            self.assertTrue(changelock.holds("test.example"))
            self.assertEqual(self.refs(), ["lock/test.example/1"])
        self.assertFalse(changelock.holds("test.example"))
        self.assertEqual(self.refs(), ["lock/test.example/1", "release/test.example/1"])

    def test_enforce_denies_when_another_agent_holds_the_lock(self):
        os.environ["AXIOM_CC_MODE"] = "enforce"
        self.c.lock("test.example", "AXIOM-SEARCH", "other")
        with changelock.site_write("test.example", "t") as ok:
            self.assertFalse(ok)
        self.assertTrue(self.c.state("test.example")["held"])        # never breaks the other lock
        self.assertEqual(self.c.state("test.example")["holder"]["owner"], "AXIOM-SEARCH")

    def test_enforce_fails_closed_when_ledger_unreachable_unless_fail_open(self):
        os.environ["AXIOM_CC_MODE"] = "enforce"
        self.st.down = True
        with changelock.site_write("test.example", "t") as ok:
            self.assertFalse(ok)
        os.environ["AXIOM_CC_FAIL_OPEN"] = "1"
        with changelock.site_write("test.example", "t") as ok:
            self.assertTrue(ok)
            changelock.guard_update("test.example")                    # fail-open grant passes the WP guard

    def test_observe_never_blocks_but_logs(self):
        self.c.lock("test.example", "AXIOM-SEARCH", "other")
        with changelock.site_write("test.example", "t") as ok:
            self.assertTrue(ok)
        self.assertTrue(any("OBSERVE" in m and "block" in m for m in self.logs))

    def test_off_makes_no_github_calls(self):
        os.environ["AXIOM_CC_MODE"] = "off"
        self.st.down = True
        with changelock.site_write("test.example", "t") as ok:
            self.assertTrue(ok)
        self.assertEqual(self.st.refs, {})

    def test_wp_update_guard(self):
        os.environ["AXIOM_CC_MODE"] = "enforce"
        with self.assertRaises(PermissionError):
            changelock.guard_update("test.example")
        with changelock.site_write("test.example", "t"):
            changelock.guard_update("test.example")
        os.environ["AXIOM_CC_MODE"] = "observe"
        changelock.guard_update("test.example")

    def test_token_is_scrubbed(self):
        mock.patch.dict(os.environ, {"AXIOM_CC_TOKEN": TOKEN}).start()
        changelock._token()
        self.assertIn(TOKEN, util.SECRETS)
        self.assertNotIn(TOKEN, util.scrub(f"Authorization: Bearer {TOKEN}"))

    def test_unrelated_github_token_is_not_used(self):
        mock.patch.dict(os.environ, {"GITHUB_TOKEN": "general-token-ABCDEF"}).start()
        mock.patch.object(changelock.config, "CC_TOKEN_FILE", changelock.config.OUT / "no-such-token-file").start()
        self.assertIsNone(changelock._token())
        mock.patch.stopall()

    def _run_apply(self, settings):
        seen = []

        def fake_apply_one(db, lk, wpc):
            seen.append((lk["id"], changelock.holds(lk["site"])))
            return True, "applied"
        db = mock.MagicMock()
        db.execute.return_value = []
        reports = []
        lks = [{"id": "a", "site": "test.example", "status": "approved", "source": "s", "target": "t", "anchor": "x"},
               {"id": "b", "site": "test.example", "status": "approved", "source": "s2", "target": "t", "anchor": "x"}]
        with mock.patch.object(links, "_apply_one", fake_apply_one), mock.patch.object(links, "_log_change"):
            links.apply(db, lks, settings, {"test.example": object()}, lambda *a: reports.append(a))
        return seen, reports

    def test_links_apply_writes_inside_one_lock_per_site(self):
        seen, reports = self._run_apply({"change_lock_mode": "enforce", "links_per_site_per_day": 5})
        self.assertEqual(seen, [("a", True), ("b", True)])
        self.assertEqual(self.refs(), ["lock/test.example/1", "release/test.example/1"])
        self.assertEqual([r[1] for r in reports], ["applied", "applied"])

    def test_links_apply_skips_without_reporting_failure_when_locked_elsewhere(self):
        self.c.lock("test.example", "HERMES", "other")
        seen, reports = self._run_apply({"change_lock_mode": "enforce", "links_per_site_per_day": 5})
        self.assertEqual(seen, [])
        self.assertEqual(reports, [])        # stays approved, retried next tick

    def test_links_apply_no_lock_churn_when_nothing_to_write(self):
        seen, _ = self._run_apply({"change_lock_mode": "enforce", "links_per_site_per_day": 0})
        self.assertEqual((seen, self.st.refs), ([], {}))


if __name__ == "__main__":
    unittest.main()
