# -*- coding: utf-8 -*-
"""The PC's part in running unattended (Windows only; a no-op elsewhere).

keep_awake     While a run holds the run lock, Windows is told the system is needed, so a PC woken by the
               "Index Engine (wake)" task is not put back to sleep after Windows' ~2-minute unattended timeout.
sync_wake_task The wake task fires one minute after the dashboard's daily run time. When the owner changes that time,
               the task is moved with it; nothing is done when the task does not exist (the owner never set it up).
"""
import subprocess
import sys
from pathlib import Path

from . import config, store
from .util import log

ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001
WAKE_TASK = "Index Engine (wake)"


def keep_awake(on):
    """True when Windows accepted the request."""
    if sys.platform != "win32":
        return False
    try:
        import ctypes
        flags = ES_CONTINUOUS | (ES_SYSTEM_REQUIRED if on else 0)
        return bool(ctypes.windll.kernel32.SetThreadExecutionState(flags))
    except Exception:
        return False


def wake_time(daily):
    """"10:30" -> "10:31"; "23:59" -> "00:00"."""
    h, m = (int(x) for x in daily.split(":"))
    t = (h * 60 + m + 1) % (24 * 60)
    return f"{t // 60:02d}:{t % 60:02d}"


def _is_engine_db(db):
    """True only for the engine's own database file (not the self-test's temporary one, not an in-memory one)."""
    try:
        path = db.execute("PRAGMA database_list").fetchone()[2]
        return bool(path) and Path(path).resolve() == (config.APP_DIR / "out" / "index_engine.db").resolve()
    except Exception:
        return False


def sync_wake_task(db, daily, runner=subprocess.run, platform=None, live=None):
    """Moves the wake task to one minute after `daily` when that changed. Returns the new time, or None.
    Only the engine's own database counts (live): the self-test runs a tick on this PC against a temporary one."""
    if live is None:
        live = _is_engine_db(db)
    if (platform or sys.platform) != "win32" or not live:
        return None
    try:
        want = wake_time(daily)
    except (ValueError, AttributeError):
        return None
    if store.get_meta(db, "__engine__").get("wake_task_time") == want:
        return None
    ps = (f"$t = Get-ScheduledTask -TaskName '{WAKE_TASK}' -ErrorAction SilentlyContinue; "
          f"if ($t) {{ Set-ScheduledTask -TaskName '{WAKE_TASK}' -Trigger (New-ScheduledTaskTrigger -Daily -At '{want}') "
          f"| Out-Null; 'moved' }} else {{ 'absent' }}")
    try:
        r = runner(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps], capture_output=True, text=True,
                   timeout=60)
    except Exception as e:
        log(f"[power] could not check the wake task: {type(e).__name__}")
        return None
    out = (r.stdout or "").strip()
    if r.returncode != 0 or out not in ("moved", "absent"):
        log(f"[power] could not move the wake task to {want}: {(r.stderr or out)[:160]}")
        return None
    store.set_meta(db, "__engine__", wake_task_time=want)
    if out == "moved":
        log(f"[power] wake task '{WAKE_TASK}' set to {want}, one minute after the daily run time")
        return want
    return None
