import sqlite3
import unittest
from unittest.mock import patch

from engine import audit, fresh, store
from engine.util import now_iso


class FreshReliabilityTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(':memory:')
        self.db.row_factory = sqlite3.Row
        self.db.executescript(store.SCHEMA)
        for migration in store.MIGRATIONS:
            self.db.execute(migration)
        self.site = 'example.test'
        self.url = 'https://example.test/post/'
        self.old = '2026-01-01T00:00:00+00:00'
        self.changed = '2026-01-02T00:00:00+00:00'
        store.set_meta(self.db, self.site, wp=True, fresh_watermark=self.old)
        self.db.execute('INSERT INTO urls(url, site, in_wp, lastmod) VALUES(?,?,1,?)',
                        (self.url, self.site, self.old))
        self.db.execute('INSERT INTO audits(url, status, title, checked_at, lastmod_at_check) VALUES(?,?,?,?,?)',
                        (self.url, 200, 'previous audit', now_iso(), self.old))
        self.db.commit()
        self.changes = {self.site: [(self.url, 'post', self.changed, None)]}

    def tearDown(self):
        self.db.close()

    def test_transient_fetch_failure_preserves_audit_and_watermark(self):
        for rec in ({'status': None, 'error': 'Timeout'},
                    {'status': 503, 'error': None},
                    {'status': 429, 'error': None},
                    {'status': 403, 'error': 'bot challenge'},
                    {'status': 200, 'error': 'parse: invalid'}):
            with self.subTest(rec=rec), patch.object(audit, 'fetch_page', return_value=rec):
                with self.assertRaisesRegex(RuntimeError, 'fresh audit failed'):
                    fresh.ingest(self.db, self.changes)
                # The caller marks the run as failed and commits; even then the old
                # audit remains intact and the next scan can retry this change.
                self.db.commit()
                row = self.db.execute('SELECT status, title FROM audits WHERE url=?', (self.url,)).fetchone()
                self.assertEqual((row['status'], row['title']), (200, 'previous audit'))
                self.assertEqual(store.get_meta(self.db, self.site)['fresh_watermark'], self.old)
                self.assertEqual(len(audit.due_rows(self.db, [self.site])[self.site]), 1)

    def test_success_and_permanent_404_are_processed(self):
        for status in (200, 404):
            with self.subTest(status=status), patch.object(audit, 'fetch_page', return_value={'status': status, 'error': None}), \
                    patch.object(audit, 'save') as save:
                self.assertEqual(fresh.ingest(self.db, self.changes), [self.url])
                save.assert_called_once()
        fresh.acknowledge(self.db, self.changes)
        self.assertEqual(store.get_meta(self.db, self.site)['fresh_watermark'], self.changed)

    def test_failed_daily_audits_remain_due_even_when_fresh(self):
        for error, status, expected in [('Timeout', None, True), ('bot challenge', 403, True),
                                        (None, 503, True), (None, 429, True),
                                        (None, 404, False), (None, 200, False)]:
            with self.subTest(error=error, status=status):
                self.db.execute('UPDATE audits SET error=?, status=?, checked_at=?, lastmod_at_check=? WHERE url=?',
                                (error, status, now_iso(), self.old, self.url))
                self.db.execute('UPDATE urls SET lastmod=? WHERE url=?', (self.old, self.url))
                self.assertEqual(bool(audit.due_rows(self.db, [self.site]).get(self.site)), expected)


if __name__ == '__main__':
    unittest.main()
