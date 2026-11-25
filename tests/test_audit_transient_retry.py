"""A transient 429/5xx or dropped connection is retried once, so it is not recorded (and alerted) as the page's state."""
import unittest
from unittest.mock import patch

import requests

from engine import audit


class FakeRaw:
    def __init__(self, body):
        self.body = body

    def read(self, n, decode_content=True):
        return self.body[:n]


class FakeResp:
    def __init__(self, status, body=b"<html><head><title>T</title></head><body><p>ok</p></body></html>", headers=None):
        self.status_code, self.raw, self.headers = status, FakeRaw(body), headers or {"Content-Type": "text/html"}
        self.url, self.history, self.encoding = "https://example.com/a", [], None

    def close(self):
        pass


class Rp:
    def can_fetch(self, agent, url):
        return True


class TransientRetry(unittest.TestCase):
    def run_with(self, responses):
        calls = []

        def fake_get(url, **kw):
            calls.append(url)
            nxt = responses.pop(0)
            if isinstance(nxt, Exception):
                raise nxt
            return nxt
        with patch.object(audit, "get", side_effect=fake_get), patch.object(audit.time, "sleep") as sleep:
            rec = audit.fetch_page("https://example.com/a", Rp())
        return rec, calls, sleep

    def test_503_then_200_records_the_page(self):
        err = FakeResp(503, b'<html><head><meta name="robots" content="noindex"></head></html>')
        rec, calls, sleep = self.run_with([err, FakeResp(200)])
        self.assertEqual((rec["status"], rec["noindex"], len(calls)), (200, 0, 2))
        sleep.assert_called_once_with(audit.RETRY_WAIT_DEFAULT)

    def test_retry_after_is_honoured_and_capped(self):
        h = {"Content-Type": "text/html", "Retry-After": "120"}
        rec, calls, sleep = self.run_with([FakeResp(429, headers=h), FakeResp(200)])
        self.assertEqual(rec["status"], 200)
        sleep.assert_called_once_with(audit.RETRY_WAIT_MAX)

    def test_persistent_503_is_recorded_after_one_retry(self):
        rec, calls, _ = self.run_with([FakeResp(503), FakeResp(503)])
        self.assertEqual((rec["status"], len(calls)), (503, 2))

    def test_404_and_challenge_are_not_retried(self):
        rec, calls, sleep = self.run_with([FakeResp(404)])
        self.assertEqual((rec["status"], len(calls)), (404, 1))
        ch = FakeResp(403, b"<html>Just a moment...</html>")
        rec, calls, sleep = self.run_with([ch])
        self.assertEqual((rec["error"], len(calls)), ("bot challenge", 1))
        sleep.assert_not_called()

    def test_connection_reset_is_retried_once_read_timeout_is_not(self):
        rec, calls, _ = self.run_with([requests.exceptions.ConnectionError("reset"), FakeResp(200)])
        self.assertEqual((rec["status"], len(calls)), (200, 2))
        rec, calls, _ = self.run_with([requests.exceptions.ReadTimeout("slow")])
        self.assertEqual((rec["status"], len(calls)), (None, 1))
        self.assertTrue(rec["error"].startswith("ReadTimeout"))


if __name__ == "__main__":
    unittest.main()
