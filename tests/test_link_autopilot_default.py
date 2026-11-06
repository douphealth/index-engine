"""Link autopilot is on unless the owner switched it off; strict sites always wait for approval."""
import sqlite3
import unittest
from unittest.mock import patch

from engine import links, store


def link(site, status="proposed", n=1):
    return {"id": f"{n:016x}", "site": site, "source": f"https://{site}/s/", "target": f"https://{site}/t{n}/",
            "anchor": "anchor words", "status": status}


class AutopilotDefaultTest(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        self.db.row_factory = sqlite3.Row
        self.db.executescript(store.SCHEMA)
        self.applied = []
        for target, fake in (("_apply_one", lambda db, lk, wpc: (self.applied.append(lk["id"]) or True, "ok")),
                             ("_log_change", lambda *a: None)):
            p = patch.object(links, target, side_effect=fake)
            p.start()
            self.addCleanup(p.stop)

    def apply(self, settings, items):
        clients = {lk["site"]: object() for lk in items}
        return links.apply(self.db, items, settings, clients, lambda *a: None)

    def test_on_when_never_set(self):
        self.apply({}, [link("plantastichaven.com")])
        self.assertEqual(self.applied, [f"{1:016x}"])

    def test_off_when_switched_off(self):
        self.apply({"link_autopilot": False}, [link("plantastichaven.com")])
        self.assertEqual(self.applied, [])

    def test_strict_site_waits_for_approval_but_approved_links_apply(self):
        self.apply({}, [link("gearuptofit.com", n=1), link("gearuptofit.com", "approved", n=2)])
        self.assertEqual(self.applied, [f"{2:016x}"])

    def test_daily_cap_still_applies(self):
        self.apply({"links_per_site_per_day": 2}, [link("plantastichaven.com", n=i) for i in range(1, 5)])
        self.assertEqual(len(self.applied), 2)


if __name__ == "__main__":
    unittest.main()
