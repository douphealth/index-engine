# -*- coding: utf-8 -*-
"""AXIOM change control gate: hold the site's global write lock around every WordPress content write.

Modes (the stricter of the AXIOM_CC_MODE env var and the dashboard setting `change_lock_mode` wins):
  off      - no checks (the old behaviour).
  observe  - DEFAULT. Takes and releases the lock when it can and logs what enforce would have blocked,
             but never blocks a write. Safe to merge: behaviour is unchanged.
  enforce  - writes only while this engine holds the site lock. Another holder -> the write is skipped
             (left approved, retried next tick). Ledger unreachable / no token -> skipped (fail closed),
             unless AXIOM_CC_FAIL_OPEN=1 (or setting change_lock_fail_open) is set.
WPClient.update also refuses, in enforce mode, any write for a site whose lock this process does not hold,
so one-off fixes/ scripts are covered too once they run with AXIOM_CC_MODE=enforce.
Stale locks are only reported, never broken.
"""
import contextlib
import os
import socket
import threading

from . import config
from . import axiom_cc as cc
from .util import SECRETS, log

MODES = ("off", "observe", "enforce")
_held = {}                  # site -> seq held by this process
_mx = threading.Lock()
_settings_mode = [""]       # last dashboard setting seen, so WPClient.update applies the same mode


def owner():
    return f"index-engine@{socket.gethostname()[:40]}"


def mode(settings=None):
    env = (os.environ.get("AXIOM_CC_MODE") or "").strip().lower()
    if settings is not None:
        _settings_mode[0] = str(settings.get("change_lock_mode") or "").strip().lower()
    st = _settings_mode[0]
    picked = [m for m in (env, st) if m in MODES]
    return max(picked, key=MODES.index) if picked else "observe"


def fail_open(settings=None):
    return os.environ.get("AXIOM_CC_FAIL_OPEN") == "1" or bool((settings or {}).get("change_lock_fail_open"))


def _token():
    tok = os.environ.get("AXIOM_CC_TOKEN")          # deliberately NOT a general GITHUB_TOKEN: use a dedicated narrow token
    if not tok:
        try:
            tok = config.CC_TOKEN_FILE.read_text(encoding="utf-8").strip().split()[-1]
        except (OSError, IndexError):
            tok = None
    if tok:
        SECRETS.append(tok)
    return tok


def client():
    c = cc.Client(repo=config.CC_REPO, issue=config.CC_LEDGER_ISSUE)
    c._token = _token()                            # never fall back to an unrelated GITHUB_TOKEN in the environment
    return c


def holds(site):
    with _mx:
        return cc.norm_site(site) in _held


@contextlib.contextmanager
def site_write(site, task, scope="", settings=None, _client=None):
    """with site_write(site, task, scope, settings) as allowed: write only if allowed."""
    m = mode(settings)
    if m == "off":
        yield True
        return
    key = cc.norm_site(site)
    c = _client or client()
    got, allowed, why = None, False, ""
    try:
        code, out = c.lock(key, owner(), task, scope)
        if code == cc.OK:
            got, allowed = out["seq"], True
        else:
            why = f"lock held by {out.get('held_by') or out.get('why')}"
    except cc.LedgerUnavailable as e:
        why = f"ledger unavailable ({e})"
        allowed = fail_open(settings)
    if not got:
        if m == "observe":
            log(f"[change-lock] OBSERVE {key}: enforce would {'allow' if allowed else 'block'} this write: {why}")
            allowed = True
        else:
            log(f"[change-lock] ENFORCE {key}: {'allowed (fail-open)' if allowed else 'BLOCKED, drafts only'}: {why}")
    if got:
        log(f"[change-lock] {key}: lock #{got} taken for {task}")
    if allowed and (got or m == "enforce"):          # enforce + fail-open grant also lets WPClient.update through
        with _mx:
            _held[key] = got or "fail-open"
    try:
        yield allowed
    finally:
        with _mx:
            _held.pop(key, None)
        if got:
            try:
                code, out = c.release(key, owner(), task, "index-engine batch finished")
                log(f"[change-lock] {key}: lock #{got} released ({out.get('result')})")
            except cc.LedgerUnavailable as e:
                log(f"[change-lock] {key}: COULD NOT RELEASE lock #{got} ({e}); it will show as stale - release it by hand")


def guard_update(site):
    """Called by WPClient.update before any content write."""
    m = mode()
    if m == "off" or holds(site):
        return
    if m == "enforce":
        raise PermissionError(f"AXIOM change control: no site lock held for {site}; WordPress write refused")
    log(f"[change-lock] OBSERVE {site}: WordPress write without the site lock (enforce would refuse it)")
