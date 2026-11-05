# -*- coding: utf-8 -*-
"""Self-update: the PC adopts new engine code from origin/main by itself, but only code that passes the full offline
test suite on this PC, and it goes back to the previous version by itself when the new one keeps failing.

Every gate must hold, or nothing changes:
  * git is installed, this folder is a git checkout on branch main, and no tracked file has local edits;
  * origin/main is a fast-forward of the commit running now (never a merge, rebase or force);
  * any new requirement in requirements.txt is already installed (a new dependency needs the owner);
  * the candidate passes `python -m unittest discover -s tests` and `python indexengine.py selftest`, run from a
    separate git worktree so the running copy is untouched while it is tested.
A commit that fails verification is remembered and never tried again. After an update, if two runs fail with the
new code before any succeeds, the previous commit is restored and the new one rejected.

The owner can turn it off in the dashboard (Settings > Engine updates). It never touches credentials, data or the
sites; it only changes this folder's code, and only to a commit already on the repository's main branch.
"""
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

from . import alerts, config
from .util import days_since, log, now_iso, parse_ts

CHECK_EVERY_MIN = 60
VERIFY_TIMEOUT_S = 900
ROLLBACK_AFTER_FAILURES = 2
WINDOWS_GIT = (r"C:\Program Files\Git\cmd\git.exe", r"C:\Program Files (x86)\Git\cmd\git.exe")


def git_exe():
    found = shutil.which("git")
    if found:
        return found
    return next((p for p in WINDOWS_GIT if os.path.exists(p)), None)


class Updater:
    def __init__(self, repo=None, state_file=None, verify=None, branch="main", remote="origin"):
        self.repo = Path(repo or config.APP_DIR)
        self.state_file = Path(state_file or (config.OUT / "update_state.json"))
        self.verify = verify or self.run_tests
        self.branch, self.remote = branch, remote
        self.git_path = git_exe()

    # ---------------------------------------------------------------- plumbing
    def git(self, *args, timeout=120, check=True):
        r = subprocess.run([self.git_path, *args], cwd=str(self.repo), capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=timeout)
        if check and r.returncode != 0:
            raise RuntimeError(f"git {args[0]} failed: {(r.stderr or r.stdout).strip()[:200]}")
        return r

    def load(self):
        try:
            return json.loads(self.state_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def save(self, state):
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        self.state_file.write_text(json.dumps(state, indent=1), encoding="utf-8")

    def usable(self):
        """Why self-update cannot work here, or None."""
        if not self.git_path:
            return "git is not installed or not on PATH"
        if not (self.repo / ".git").exists():
            return f"{self.repo} is not a git checkout"
        branch = self.git("rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
        if branch != self.branch:
            return f"the checkout is on '{branch}', not '{self.branch}'"
        if self.git("status", "--porcelain", "--untracked-files=no").stdout.strip():
            return "tracked files have local edits (commit or discard them to let the engine update itself)"
        return None

    def head(self):
        return self.git("rev-parse", "HEAD").stdout.strip()

    def missing_requirements(self, sha):
        """Requirements of the candidate that are not installed here (names only; the tests check versions)."""
        from importlib import metadata
        text = self.git("show", f"{sha}:requirements.txt", check=False).stdout
        missing = []
        for line in text.splitlines():
            name = re.split(r"[<>=!~;\[\s]", line.strip(), maxsplit=1)[0]
            if not name or name.startswith("#"):
                continue
            try:
                metadata.version(name)
            except metadata.PackageNotFoundError:
                missing.append(name)
        return missing

    def run_tests(self, sha):
        """Run the offline test suites against `sha` in a separate worktree. Returns (ok, tail of output)."""
        work = self.state_file.parent / "update-candidate"
        self.git("worktree", "prune", check=False)
        if work.exists():
            self.git("worktree", "remove", "--force", str(work), check=False)
            shutil.rmtree(work, ignore_errors=True)
        self.git("worktree", "add", "--force", "--detach", str(work), sha)
        env = {k: v for k, v in os.environ.items() if not k.startswith("INDEX_ENGINE_")}
        env.update(PYTHONDONTWRITEBYTECODE="1", PYTHONIOENCODING="utf-8", INDEX_ENGINE_NO_UPDATE="1",
                   INDEX_ENGINE_CREDS=str(work / "no-credentials.txt"),                # the tests never see real ones
                   INDEX_ENGINE_DASHBOARD_FILE=str(work / "no-dashboard.json"),
                   INDEX_ENGINE_CF_TOKEN_FILE=str(work / "no-token.txt"))
        out = []
        try:
            for cmd in ([sys.executable, "-m", "unittest", "discover", "-s", "tests"], [sys.executable, "indexengine.py", "selftest"]):
                try:
                    r = subprocess.run(cmd, cwd=str(work), env=env, capture_output=True, text=True, encoding="utf-8",
                                       errors="replace", timeout=VERIFY_TIMEOUT_S)
                except subprocess.TimeoutExpired:
                    return False, f"{' '.join(cmd[1:])} timed out after {VERIFY_TIMEOUT_S} s"
                out.append((r.stdout + r.stderr)[-600:])
                if r.returncode != 0:
                    return False, f"{' '.join(cmd[1:])} failed (exit {r.returncode}): " + out[-1][-400:]
            return True, out[-1][-200:]
        finally:
            self.git("worktree", "remove", "--force", str(work), check=False)
            shutil.rmtree(work, ignore_errors=True)

    # ---------------------------------------------------------------- the two decisions
    def check(self, db=None, force=False):
        """Adopt origin/main when every gate holds. Returns 'updated', 'current', 'skipped' or 'blocked'."""
        if os.environ.get("INDEX_ENGINE_NO_UPDATE"):
            return "skipped"                           # set while a candidate is being tested: never nest updates
        state = self.load()
        if not force and state.get("checked_at") and (days_since(state["checked_at"]) or 0) * 24 * 60 < CHECK_EVERY_MIN:
            return "skipped"
        state["checked_at"] = now_iso()
        try:
            why = self.usable()
            if why:
                return self._blocked(db, state, why)
            self.git("fetch", "--quiet", self.remote, self.branch, timeout=180)
            head, cand = self.head(), self.git("rev-parse", f"{self.remote}/{self.branch}").stdout.strip()
            if cand == head:
                state.pop("blocked", None)
                return self._done(state, "current")
            if cand in (state.get("rejected") or []):
                return self._done(state, "current")
            if self.git("merge-base", "--is-ancestor", head, cand, check=False).returncode != 0:
                return self._blocked(db, state, f"{self.remote}/{self.branch} is not a fast-forward of the running "
                                                 f"commit {head[:7]}: update by hand")
            missing = self.missing_requirements(cand)
            if missing:
                return self._blocked(db, state, f"the new version needs {', '.join(missing)}: run "
                                                 "`pip install -r requirements.txt`, then it updates itself")
            log(f"[update] testing {cand[:7]} (running {head[:7]}) before adopting it")
            ok, detail = self.verify(cand)
            if not ok:
                state.setdefault("rejected", []).append(cand)
                state["rejected"] = state["rejected"][-20:]
                log(f"[update] {cand[:7]} failed its tests and will not be used: {detail[-300:]}")
                if db is not None:
                    alerts.add(db, "", None, "engine_update_rejected", "warn",
                               f"The engine did not update to {cand[:7]}: its tests failed on this PC. "
                               f"It keeps running {head[:7]}. {detail[-300:]}")
                return self._done(state, "rejected")
            self.git("merge", "--ff-only", "--quiet", cand)
            state.update(updated_from=head, updated_to=cand, updated_at=now_iso(), confirmed=False)
            state.pop("blocked", None)
            log(f"[update] engine updated {head[:7]} -> {cand[:7]} after its tests passed on this PC")
            if db is not None:
                alerts.add(db, "", None, "engine_updated", "good",
                           f"The engine updated itself to {cand[:7]} (from {head[:7]}) after the full test suite passed "
                           "on this PC. It rolls back by itself if the new version keeps failing.")
            return self._done(state, "updated")
        except Exception as e:
            log(f"[update] check failed: {type(e).__name__}: {str(e)[:200]}")
            return self._done(state, "error")

    def watch(self, db):
        """After an update: confirm it on the first successful run, or roll back after repeated failures."""
        state = self.load()
        if not state.get("updated_at") or state.get("confirmed"):
            return None
        since = parse_ts(state["updated_at"])
        runs = [r for r in db.execute("SELECT started_at, status FROM local_runs")
                if (parse_ts(r["started_at"]) or since) > since and r["status"] in ("ok", "failed")]
        if any(r["status"] == "ok" for r in runs):
            state["confirmed"] = True
            self.save(state)
            return "confirmed"
        if sum(r["status"] == "failed" for r in runs) < ROLLBACK_AFTER_FAILURES:
            return None
        bad, good = state.get("updated_to"), state.get("updated_from")
        try:
            if self.head() != bad:
                state["confirmed"] = True          # someone changed the code by hand since: not ours to undo
                self.save(state)
                return None
            self.git("reset", "--keep", good)
        except Exception as e:
            log(f"[update] rollback failed: {type(e).__name__}: {str(e)[:200]}")
            return "error"
        state.setdefault("rejected", []).append(bad)
        state.update(confirmed=True, rolled_back_at=now_iso())
        self.save(state)
        log(f"[update] {ROLLBACK_AFTER_FAILURES} runs failed on {bad[:7]} and none succeeded: rolled back to {good[:7]}")
        alerts.add(db, "", None, "engine_rolled_back", "high",
                   f"The engine went back to {good[:7]}: {ROLLBACK_AFTER_FAILURES} runs failed on {bad[:7]} and none "
                   "succeeded. That version will not be used again; the next commit on main is tested as usual.")
        db.commit()
        return "rolled_back"

    def _blocked(self, db, state, why):
        if state.get("blocked") != why:
            log(f"[update] self-update is off on this PC: {why}")
            if db is not None:
                alerts.add(db, "", None, "engine_update_blocked", "info", f"The engine cannot update itself: {why}.")
        state["blocked"] = why
        return self._done(state, "blocked")

    def _done(self, state, result):
        state["last_result"] = result
        self.save(state)
        return result


_VERSION = None


def version():
    """Short commit of the running code ('' when unknown). Sent with the heartbeat."""
    global _VERSION
    if _VERSION is None:
        try:
            u = Updater()
            _VERSION = u.head()[:7] if u.git_path and (u.repo / ".git").exists() else ""
        except Exception:
            _VERSION = ""
    return _VERSION
