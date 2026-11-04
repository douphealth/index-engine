#!/usr/bin/env python3
# Vendored from the AXIOM change-control client (protocol axiom-cc v1). Keep in sync; stdlib only.
"""AXIOM change control: GitHub-backed per-site write locks and an append-only change ledger.

Protocol v1 (create-only, so every step is atomic and auditable):
  * lock    = create ref  refs/heads/axiom-cc/lock/<site>/<n>      (GitHub answers 422 if it exists)
  * release = create ref  refs/heads/axiom-cc/release/<site>/<n>
  * a site is HELD when its highest lock number n has no matching release ref
  * who/why/scope/timestamps live in the ledger: JSON comments on one GitHub issue
Nothing is ever deleted, so the refs and the issue are the full audit trail.

Auth: GITHUB_TOKEN (or AXIOM_CC_TOKEN) from the environment only; never printed or stored.
Config: AXIOM_CC_REPO (owner/name), AXIOM_CC_LEDGER_ISSUE (issue number),
        AXIOM_CC_STALE_HOURS (default 6).
Stdlib only, Python 3.8+.

CLI:
  axiom_cc.py lock    <site> <owner> <task> [scope]
  axiom_cc.py release <site> <owner> <task> [evidence]
  axiom_cc.py record  <event> <site> <owner> <task> [detail]   # release_shipped|rollback|draft
  axiom_cc.py status  [site]       # held locks + stale report (never breaks locks)
Exit codes: 0 ok, 2 lock held by someone else / race lost, 3 not the owner / nothing to release,
            4 ledger unavailable (fail closed: drafts only), 1 usage.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

VERSION = "axiom-cc v1"
DEFAULT_REPO = "douphealth/hermes-config-001"
API = os.environ.get("AXIOM_CC_API", "https://api.github.com")
OK, DENIED, NOT_OWNER, UNAVAILABLE = 0, 2, 3, 4


class LedgerUnavailable(Exception):
    """GitHub unreachable or refused us: callers must fail closed (no production writes)."""


def norm_site(site: str) -> str:
    s = (site or "").strip().lower()
    s = re.sub(r"^[a-z]+://", "", s).split("/")[0].split(":")[0]
    s = s[4:] if s.startswith("www.") else s
    if not re.fullmatch(r"[a-z0-9][a-z0-9.-]*[a-z0-9]", s) or ".." in s or s.endswith(".lock"):
        raise ValueError(f"not a valid site name: {site!r}")
    return s


def now_iso() -> str:
    return dt.datetime.now(dt.UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


class Client:
    def __init__(
        self,
        repo: str | None = None,
        issue: int | str | None = None,
        token: str | None = None,
        api: str | None = None,
        timeout: float = 20,
    ) -> None:
        self.repo = repo or os.environ.get("AXIOM_CC_REPO") or DEFAULT_REPO
        issue = issue if issue is not None else os.environ.get("AXIOM_CC_LEDGER_ISSUE")
        self.issue = int(issue) if issue else None
        self._token = token or os.environ.get("AXIOM_CC_TOKEN") or os.environ.get("GITHUB_TOKEN")
        self.api = (api or API).rstrip("/")
        self.timeout = timeout

    # ---------------------------------------------------------------- HTTP
    def _req(
        self, method: str, path: str, body: Any = None, ok: tuple[int, ...] = (200, 201)
    ) -> tuple[int, Any, Any]:
        if not self._token:
            raise LedgerUnavailable("no GITHUB_TOKEN/AXIOM_CC_TOKEN in the environment")
        if not self.issue:
            raise LedgerUnavailable("AXIOM_CC_LEDGER_ISSUE is not set")
        req = urllib.request.Request(
            self.api + path,
            method=method,
            data=json.dumps(body).encode() if body is not None else None,
            headers={
                "Authorization": "Bearer " + self._token,
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "axiom-cc",
                "Content-Type": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                return r.status, json.loads(r.read() or b"null"), r.headers
        except urllib.error.HTTPError as e:
            if e.code in ok or e.code == 422:
                return e.code, None, e.headers
            raise LedgerUnavailable(
                f"GitHub {method} {path.split('?')[0]} -> HTTP {e.code}"
            ) from None
        except (urllib.error.URLError, OSError, ValueError) as e:
            raise LedgerUnavailable(f"GitHub unreachable ({type(e).__name__})") from None

    def _paged(self, path: str) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        page = 1
        while True:
            sep = "&" if "?" in path else "?"
            _, data, _ = self._req("GET", f"{path}{sep}per_page=100&page={page}")
            data = data or []
            out.extend(data)
            if len(data) < 100:
                return out
            page += 1

    # ---------------------------------------------------------------- state
    def _seqs(self, kind: str, site: str) -> set[int]:
        refs = self._paged(f"/repos/{self.repo}/git/matching-refs/heads/axiom-cc/{kind}/{site}/")
        out = set()
        for r in refs:
            tail = r.get("ref", "").rsplit("/", 1)[-1]
            if tail.isdigit():
                out.add(int(tail))
        return out

    def ledger(self) -> list[dict[str, Any]]:
        entries = []
        for c in self._paged(f"/repos/{self.repo}/issues/{self.issue}/comments"):
            m = re.search(r"```json\s*(\{.*?\})\s*```", c.get("body") or "", re.S)
            if m and VERSION in (c.get("body") or ""):
                try:
                    e = json.loads(m.group(1))
                except ValueError:
                    continue
                e["_by"] = (c.get("user") or {}).get("login")
                e["_at"] = c.get("created_at")
                entries.append(e)
        return entries

    def state(self, site: str) -> dict[str, Any]:
        site = norm_site(site)
        locks, rels = self._seqs("lock", site), self._seqs("release", site)
        top = max(locks) if locks else 0
        held = bool(top) and top not in rels
        holder = None
        if held:
            holder = next(
                (
                    e
                    for e in reversed(self.ledger())
                    if e.get("event") == "lock" and e.get("site") == site and e.get("seq") == top
                ),
                None,
            )
        return {"site": site, "seq": top, "held": held, "holder": holder}

    def _base_sha(self) -> str:
        _, repo, _ = self._req("GET", f"/repos/{self.repo}")
        _, ref, _ = self._req("GET", f"/repos/{self.repo}/git/ref/heads/{repo['default_branch']}")
        return str(ref["object"]["sha"])

    def _comment(self, entry: dict[str, Any]) -> None:
        head = (
            f"{VERSION} | {entry['event']} | {entry['site']} | {entry['owner']} | {entry['task']}"
        )
        body = f"{head}\n```json\n{json.dumps(entry, ensure_ascii=False)}\n```"
        self._req("POST", f"/repos/{self.repo}/issues/{self.issue}/comments", {"body": body})

    def _create_ref(self, name: str, sha: str) -> bool:
        code, _, _ = self._req(
            "POST", f"/repos/{self.repo}/git/refs", {"ref": f"refs/heads/{name}", "sha": sha}
        )
        return code == 201

    # ---------------------------------------------------------------- operations
    def lock(self, site: str, owner: str, task: str, scope: str = "") -> tuple[int, dict[str, Any]]:
        site = norm_site(site)
        st = self.state(site)
        if st["held"]:
            return DENIED, {
                "result": "DENIED",
                "site": site,
                "held_by": _who(st["holder"]),
                "seq": st["seq"],
            }
        seq = st["seq"] + 1
        if not self._create_ref(f"axiom-cc/lock/{site}/{seq}", self._base_sha()):
            return DENIED, {
                "result": "DENIED",
                "site": site,
                "why": "another agent took the lock first (ref exists)",
            }
        entry = {
            "event": "lock",
            "site": site,
            "seq": seq,
            "owner": owner,
            "task": task,
            "scope": scope,
            "ts": now_iso(),
        }
        try:
            self._comment(entry)
        except LedgerUnavailable:
            # could not record the holder: give it straight back, never leave an anonymous lock
            self._create_ref(f"axiom-cc/release/{site}/{seq}", self._base_sha())
            raise
        return OK, {"result": "LOCKED", "site": site, "seq": seq, "owner": owner, "task": task}

    def release(
        self, site: str, owner: str, task: str, evidence: str = ""
    ) -> tuple[int, dict[str, Any]]:
        site = norm_site(site)
        st = self.state(site)
        if not st["held"]:
            return NOT_OWNER, {"result": "NOT_HELD", "site": site}
        if (st["holder"] or {}).get("owner") != owner:
            return NOT_OWNER, {
                "result": "DENIED",
                "site": site,
                "why": "not the owner",
                "held_by": _who(st["holder"]),
            }
        if not self._create_ref(f"axiom-cc/release/{site}/{st['seq']}", self._base_sha()):
            return NOT_OWNER, {"result": "NOT_HELD", "site": site, "why": "already released"}
        self._comment(
            {
                "event": "release",
                "site": site,
                "seq": st["seq"],
                "owner": owner,
                "task": task,
                "detail": evidence,
                "ts": now_iso(),
            }
        )
        return OK, {"result": "RELEASED", "site": site, "seq": st["seq"]}

    def record(
        self, event: str, site: str, owner: str, task: str, detail: str = ""
    ) -> tuple[int, dict[str, Any]]:
        entry = {
            "event": event,
            "site": norm_site(site),
            "owner": owner,
            "task": task,
            "detail": detail,
            "ts": now_iso(),
        }
        self._comment(entry)
        return OK, {"result": "RECORDED", **entry}

    def status(
        self, site: str | None = None, stale_hours: float | None = None
    ) -> tuple[int, dict[str, Any]]:
        if stale_hours is None:
            stale_hours = float(os.environ.get("AXIOM_CC_STALE_HOURS") or 6)
        led = self.ledger()
        if site:
            sites = [norm_site(site)]
        else:  # every site that has ever had a lock ref, ledger entry or not
            refs = self._paged(f"/repos/{self.repo}/git/matching-refs/heads/axiom-cc/lock/")
            sites = sorted(
                {r["ref"].split("/")[-2] for r in refs if r.get("ref", "").count("/") >= 5}
            )
        held, now = [], dt.datetime.now(dt.UTC)
        for s in sites:
            st = self.state(s)
            if st["held"]:
                h = st["holder"] or {}
                age = None
                if h.get("ts"):
                    age = round(
                        (
                            now - dt.datetime.fromisoformat(h["ts"].replace("Z", "+00:00"))
                        ).total_seconds()
                        / 3600,
                        1,
                    )
                held.append(
                    {
                        "site": s,
                        "seq": st["seq"],
                        "owner": h.get("owner", "UNKNOWN (no ledger entry)"),
                        "task": h.get("task"),
                        "scope": h.get("scope"),
                        "since": h.get("ts"),
                        "age_h": age,
                        "stale": age is None or age >= stale_hours,
                    }
                )
        cut = now - dt.timedelta(days=7)
        shipped = [
            e
            for e in led
            if e.get("event") == "release_shipped"
            and e.get("ts")
            and dt.datetime.fromisoformat(e["ts"].replace("Z", "+00:00")) > cut
        ]
        return OK, {
            "held": held,
            "stale": [h for h in held if h["stale"]],
            "shipped_7d": shipped,
            "note": "stale locks are only reported; the owner (or Alex, by hand) releases them",
        }


def _who(h: dict[str, Any] | None) -> Any:
    return (
        {k: (h or {}).get(k) for k in ("owner", "task", "scope", "ts")}
        if h
        else "UNKNOWN (no ledger entry)"
    )


def main(argv: list[str] | None = None) -> int:
    a = list(sys.argv[1:] if argv is None else argv)
    usage = __doc__.split("CLI:")[1]
    if not a or a[0] not in ("lock", "release", "record", "status"):
        print(usage, file=sys.stderr)
        return 1
    c = Client()
    try:
        if a[0] == "lock" and len(a) >= 4:
            code, out = c.lock(a[1], a[2], a[3], " ".join(a[4:]))
        elif a[0] == "release" and len(a) >= 4:
            code, out = c.release(a[1], a[2], a[3], " ".join(a[4:]))
        elif a[0] == "record" and len(a) >= 5:
            code, out = c.record(a[1], a[2], a[3], a[4], " ".join(a[5:]))
        elif a[0] == "status":
            code, out = c.status(a[1] if len(a) > 1 else None)
        else:
            print(usage, file=sys.stderr)
            return 1
    except LedgerUnavailable as e:
        print(
            json.dumps(
                {
                    "result": "LEDGER_UNAVAILABLE",
                    "why": str(e),
                    "action": "ledger unavailable: drafts only",
                }
            )
        )
        return UNAVAILABLE
    except ValueError as e:
        print(json.dumps({"result": "ERROR", "why": str(e)}))
        return 1
    print(json.dumps(out, ensure_ascii=False))
    return code


if __name__ == "__main__":
    sys.exit(main())
