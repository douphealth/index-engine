"""Self-update against real git repositories: adopt only tested fast-forwards, refuse every unsafe state, roll back."""
import os
import shutil
import sqlite3
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from engine import store, update
from engine.util import now_iso

GIT = update.git_exe()


def git(cwd, *args):
    return subprocess.run([GIT, *args], cwd=str(cwd), capture_output=True, text=True, check=True).stdout.strip()


@unittest.skipUnless(GIT, "git is not installed")
class SelfUpdateTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="ie-update-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        origin, self.author = self.tmp / "origin.git", self.tmp / "author"
        git(self.tmp, "init", "--quiet", "--bare", "-b", "main", str(origin))
        git(self.tmp, "clone", "--quiet", str(origin), str(self.author))
        for repo in (self.author,):
            git(repo, "config", "user.email", "t@example.test")
            git(repo, "config", "user.name", "t")
        (self.author / "requirements.txt").write_text("requests\n")
        (self.author / "code.py").write_text("v = 1\n")
        git(self.author, "add", "-A")
        git(self.author, "commit", "--quiet", "-m", "v1")
        git(self.author, "push", "--quiet", "origin", "HEAD:main")
        self.pc = self.tmp / "pc"
        git(self.tmp, "clone", "--quiet", str(origin), str(self.pc))
        self.verified = []
        self.ok = True
        self.db = sqlite3.connect(":memory:")
        self.db.row_factory = sqlite3.Row
        self.db.executescript(store.SCHEMA)
        env = patch.dict(os.environ)                 # these tests also run inside a candidate's verification,
        env.start()                                  # where the no-nesting guard is set
        os.environ.pop("INDEX_ENGINE_NO_UPDATE", None)
        self.addCleanup(env.stop)
        log = patch.object(update, "log")
        log.start()
        self.addCleanup(log.stop)

    def updater(self):
        return update.Updater(repo=self.pc, state_file=self.tmp / "state.json",
                              verify=lambda sha: (self.verified.append(sha) or self.ok, "tests said so"))

    def publish(self, text="v = 2\n", req=None):
        (self.author / "code.py").write_text(text)
        if req is not None:
            (self.author / "requirements.txt").write_text(req)
        git(self.author, "commit", "--quiet", "-am", "next")
        git(self.author, "push", "--quiet", "origin", "HEAD:main")
        return git(self.author, "rev-parse", "HEAD")

    def head(self):
        return git(self.pc, "rev-parse", "HEAD")

    def kinds(self):
        return [r["kind"] for r in self.db.execute("SELECT kind FROM alerts")]

    def test_nothing_new_is_current(self):
        self.assertEqual(self.updater().check(self.db), "current")
        self.assertEqual(self.verified, [])

    def test_tested_fast_forward_is_adopted_and_checks_are_throttled(self):
        new = self.publish()
        u = self.updater()
        self.assertEqual(u.check(self.db), "updated")
        self.assertEqual((self.head(), self.verified), (new, [new]))
        self.assertEqual((self.pc / "code.py").read_text(), "v = 2\n")
        self.assertIn("engine_updated", self.kinds())
        self.publish("v = 3\n")
        self.assertEqual(u.check(self.db), "skipped")             # at most hourly

    def test_failing_commit_is_rejected_once_and_never_retried(self):
        old, new = self.head(), self.publish()
        self.ok = False
        u = self.updater()
        self.assertEqual(u.check(self.db), "rejected")
        self.assertEqual(self.head(), old)
        self.assertIn("engine_update_rejected", self.kinds())
        self.assertEqual(u.check(self.db, force=True), "current")
        self.assertEqual(self.verified, [new])

    def test_local_edits_block_the_update(self):
        self.publish()
        (self.pc / "code.py").write_text("edited on the PC\n")
        self.assertEqual(self.updater().check(self.db), "blocked")
        self.assertEqual((self.pc / "code.py").read_text(), "edited on the PC\n")
        self.assertIn("engine_update_blocked", self.kinds())

    def test_diverged_history_is_never_forced(self):
        git(self.pc, "config", "user.email", "t@example.test")
        git(self.pc, "config", "user.name", "t")
        (self.pc / "local.py").write_text("x\n")
        git(self.pc, "add", "-A")
        git(self.pc, "commit", "--quiet", "-m", "local")
        mine = self.head()
        self.publish()
        self.assertEqual(self.updater().check(self.db), "blocked")
        self.assertEqual(self.head(), mine)

    def test_other_branch_blocks(self):
        git(self.pc, "checkout", "--quiet", "-b", "experiment")
        self.publish()
        self.assertEqual(self.updater().check(self.db), "blocked")

    def test_new_dependency_waits_for_the_owner(self):
        self.publish(req="requests\nsurely-not-installed-package-xyz\n")
        self.assertEqual(self.updater().check(self.db), "blocked")
        self.assertEqual(self.verified, [])

    def test_disabled_while_a_candidate_is_tested(self):
        self.publish()
        with patch.dict(os.environ, {"INDEX_ENGINE_NO_UPDATE": "1"}):
            self.assertEqual(self.updater().check(self.db), "skipped")

    def run_row(self, status):
        self.db.execute("INSERT INTO local_runs(started_at, status, trigger) VALUES(?,?, 'schedule')",
                        ("2099-01-01T00:00:00+00:00", status))

    def test_rolls_back_after_repeated_failures_and_rejects_that_commit(self):
        old = self.head()
        new = self.publish()
        u = self.updater()
        u.check(self.db)
        self.run_row("failed")
        self.assertIsNone(u.watch(self.db))
        self.run_row("failed")
        self.assertEqual(u.watch(self.db), "rolled_back")
        self.assertEqual(self.head(), old)
        self.assertIn("engine_rolled_back", self.kinds())
        self.assertEqual(u.check(self.db, force=True), "current")    # the bad commit is not adopted again
        self.assertEqual(self.verified, [new])

    def test_one_success_confirms_the_update(self):
        new = self.publish()
        u = self.updater()
        u.check(self.db)
        self.run_row("ok")
        self.run_row("failed")
        self.run_row("failed")
        self.assertEqual(u.watch(self.db), "confirmed")
        self.assertIsNone(u.watch(self.db))
        self.assertEqual(self.head(), new)

    def test_real_verification_runs_tests_in_a_separate_worktree(self):
        (self.author / "tests").mkdir()
        (self.author / "tests" / "test_x.py").write_text("import unittest\nclass T(unittest.TestCase):\n    def test_ok(self): pass\n")
        (self.author / "indexengine.py").write_text("import sys\nsys.exit(0 if sys.argv[1:] == ['selftest'] else 2)\n")
        git(self.author, "add", "-A")
        git(self.author, "commit", "--quiet", "-m", "with tests")
        git(self.author, "push", "--quiet", "origin", "HEAD:main")
        u = update.Updater(repo=self.pc, state_file=self.tmp / "out" / "state.json")
        self.assertEqual(u.check(self.db), "updated")
        self.assertFalse((self.tmp / "out" / "update-candidate").exists())
        (self.author / "indexengine.py").write_text("raise SystemExit(1)\n")
        git(self.author, "commit", "--quiet", "-am", "broken selftest")
        git(self.author, "push", "--quiet", "origin", "HEAD:main")
        before = self.head()
        self.assertEqual(u.check(self.db, force=True), "rejected")
        self.assertEqual(self.head(), before)


if __name__ == "__main__":
    unittest.main()
