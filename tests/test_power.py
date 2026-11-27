"""The PC's part in running unattended (engine/power.py): stay awake during a run, keep the wake task in step."""
import sqlite3
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from engine import power, store


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


class Runner:
    def __init__(self, out="moved", code=0):
        self.calls, self.out, self.code = [], out, code

    def __call__(self, argv, **kw):
        self.calls.append(argv)
        return SimpleNamespace(returncode=self.code, stdout=self.out + "\r\n", stderr="")


class WakeTask(unittest.TestCase):
    def test_wake_time_is_one_minute_after_and_wraps(self):
        self.assertEqual(power.wake_time("10:30"), "10:31")
        self.assertEqual(power.wake_time("09:59"), "10:00")
        self.assertEqual(power.wake_time("23:59"), "00:00")

    def test_moved_once_then_only_when_the_time_changes(self):
        db, r = memory_db(), Runner()
        self.assertEqual(power.sync_wake_task(db, "10:30", runner=r, platform="win32", live=True), "10:31")
        self.assertIn("-At '10:31'", r.calls[0][-1])
        self.assertIn("Index Engine (wake)", r.calls[0][-1])
        self.assertIsNone(power.sync_wake_task(db, "10:30", runner=r, platform="win32", live=True))
        self.assertEqual(len(r.calls), 1, "no PowerShell call while the time is unchanged")
        self.assertEqual(power.sync_wake_task(db, "06:15", runner=r, platform="win32", live=True), "06:16")
        self.assertEqual(len(r.calls), 2)

    def test_no_task_is_remembered_and_not_created(self):
        db, r = memory_db(), Runner(out="absent")
        self.assertIsNone(power.sync_wake_task(db, "10:30", runner=r, platform="win32", live=True))
        self.assertNotIn("Register-ScheduledTask", r.calls[0][-1])
        power.sync_wake_task(db, "10:30", runner=r, platform="win32", live=True)
        self.assertEqual(len(r.calls), 1)

    def test_a_failure_is_retried_next_tick(self):
        db = memory_db()
        power.sync_wake_task(db, "10:30", runner=Runner(out="", code=1), platform="win32", live=True)
        r = Runner()
        self.assertEqual(power.sync_wake_task(db, "10:30", runner=r, platform="win32", live=True), "10:31")

    def test_never_outside_windows_or_the_engines_own_database(self):
        db, r = memory_db(), Runner()
        self.assertIsNone(power.sync_wake_task(db, "10:30", runner=r, platform="linux", live=True))
        self.assertIsNone(power.sync_wake_task(db, "10:30", runner=r, platform="win32", live=False))
        self.assertIsNone(power.sync_wake_task(db, "25:99x", runner=r, platform="win32", live=True))
        self.assertEqual(r.calls, [])
        self.assertIsNone(power.sync_wake_task(db, "10:30", runner=r, platform="win32"),
                          "the test database is not the engine's, so nothing is moved")


class KeepAwake(unittest.TestCase):
    def test_the_run_lock_holds_the_pc_awake_and_lets_go(self):
        import tempfile
        from pathlib import Path

        import indexengine
        from engine import config
        calls = []
        with patch.object(power, "keep_awake", side_effect=lambda on: calls.append(on) or True), \
                patch.object(config, "LOCK_FILE", Path(tempfile.mkdtemp()) / "run.lock"):
            with indexengine.RunLock():
                self.assertEqual(calls, [True])
        self.assertEqual(calls, [True, False])

    def test_off_windows_it_does_nothing(self):
        with patch.object(power.sys, "platform", "linux"):
            self.assertFalse(power.keep_awake(True))


if __name__ == "__main__":
    unittest.main()
