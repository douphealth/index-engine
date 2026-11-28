import sqlite3
from pathlib import Path
import unittest
import zlib
from unittest.mock import patch

from engine import links, store


SITE = 'example.test'
OTHER_SITE = 'other.test'
OLD_FULL = '2024-01-01T00:00:00Z'
OLD_WATERMARK = '2024-01-02T00:00:00'
NEW_WATERMARK = '2024-02-02T00:00:00'


def item(url, raw, modified=NEW_WATERMARK, post_id=1, post_type='posts'):
    return {'url': url, 'raw': raw, 'modified': modified, 'id': post_id, 'type': post_type}


class FakeWP:
    def __init__(self, items, fail=False):
        self.items = items
        self.fail = fail
        self.after = 'not called'

    def corpus(self, after=None):
        self.after = after
        yield from self.items
        if self.fail:
            raise RuntimeError('network failure')


class CorpusRefreshTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(':memory:')
        self.db.row_factory = sqlite3.Row
        self.db.executescript(store.SCHEMA)
        self.db.execute('INSERT INTO link_corpus VALUES(?,?,?,?,?,?)',
                        ('https://example.test/old', SITE, 1, 'posts', OLD_WATERMARK, zlib.compress(b'old content')))
        self.db.execute('INSERT INTO link_corpus VALUES(?,?,?,?,?,?)',
                        ('https://example.test/keep', SITE, 2, 'pages', OLD_WATERMARK, zlib.compress(b'keep content')))
        self.db.execute('INSERT INTO link_corpus VALUES(?,?,?,?,?,?)',
                        ('https://other.test/page', OTHER_SITE, 3, 'posts', OLD_WATERMARK, zlib.compress(b'other content')))
        store.set_meta(self.db, SITE, corpus_full_at=OLD_FULL, corpus_watermark=OLD_WATERMARK, unrelated='preserved')
        self.addCleanup(self.db.close)

    def snapshot(self):
        return ([tuple(row) for row in self.db.execute('SELECT * FROM link_corpus ORDER BY url')],
                [tuple(row) for row in self.db.execute('SELECT * FROM site_meta ORDER BY site')])

    def test_failed_full_refresh_rolls_back_before_and_after_first_item(self):
        for items in ([], [item('https://example.test/new', 'new content')]):
            with self.subTest(items=items), patch.object(links, 'days_since', return_value=8):
                before = self.snapshot()
                wp = FakeWP(items, fail=True)
                with self.assertRaisesRegex(RuntimeError, 'network failure'):
                    links.refresh_corpus(self.db, SITE, wp)
                self.assertIsNone(wp.after)
                self.db.commit()  # Mimic the caller's later commit after catching the error.
                self.assertEqual(self.snapshot(), before)

    def test_failed_incremental_refresh_restores_replaced_rows_and_pending_work(self):
        before = self.snapshot()
        self.db.execute("INSERT INTO manual_queue VALUES('pending', 'today')")
        wp = FakeWP([item('https://example.test/old', 'overwritten'),
                     item('https://example.test/new', 'new')], fail=True)
        with patch.object(links, 'days_since', return_value=1):
            with self.assertRaisesRegex(RuntimeError, 'network failure'):
                links.refresh_corpus(self.db, SITE, wp)
        self.assertEqual(wp.after, OLD_WATERMARK)
        self.db.commit()
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.db.execute('SELECT url FROM manual_queue').fetchone()[0], 'pending')

    def test_propose_uses_prior_corpus_after_failed_refresh(self):
        wp = FakeWP([item('https://example.test/new', 'partial')], fail=True)
        wp.ok = True
        results = {SITE: {'buckets': {'https://example.test/old': ('INDEXED', set())}, 'rows': []}}
        before = self.snapshot()
        with patch.object(links, 'days_since', return_value=8), patch.object(links, 'log') as log:
            self.assertEqual(links.propose(self.db, results, {}, {SITE: wp}), [])
        self.assertIn('using the stored copy', log.call_args_list[0].args[0])
        self.assertIn('1 trusted source pages scanned', log.call_args_list[1].args[0])
        self.db.commit()
        self.assertEqual(self.snapshot(), before)

    def test_malformed_item_after_valid_item_rolls_back(self):
        before = self.snapshot()
        wp = FakeWP([item('https://example.test/new', 'new'), {'url': 'bad'}])
        with patch.object(links, 'days_since', return_value=8):
            with self.assertRaises(KeyError):
                links.refresh_corpus(self.db, SITE, wp)
        self.db.commit()
        self.assertEqual(self.snapshot(), before)

    def test_metadata_write_failure_rolls_back(self):
        self.db.execute("CREATE TRIGGER fail_meta BEFORE UPDATE ON site_meta BEGIN SELECT RAISE(ABORT, 'meta failed'); END")
        before = self.snapshot()
        with patch.object(links, 'days_since', return_value=8):
            with self.assertRaisesRegex(sqlite3.IntegrityError, 'meta failed'):
                links.refresh_corpus(self.db, SITE, FakeWP([item('https://example.test/new', 'new')]))
        self.db.commit()
        self.assertEqual(self.snapshot(), before)

    def test_commit_failure_rolls_back_refresh_without_discarding_caller_work(self):
        self.db.execute('PRAGMA foreign_keys=ON')
        self.db.execute('CREATE TABLE parent(id INTEGER PRIMARY KEY)')
        self.db.execute('CREATE TABLE child(id INTEGER REFERENCES parent(id) DEFERRABLE INITIALLY DEFERRED)')
        before = self.snapshot()
        self.db.execute('INSERT INTO child VALUES(42)')
        with patch.object(links, 'days_since', return_value=8):
            with self.assertRaises(sqlite3.IntegrityError):
                links.refresh_corpus(self.db, SITE, FakeWP([item('https://example.test/new', 'new')]))
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.db.execute('SELECT id FROM child').fetchone()[0], 42)
        self.db.execute('INSERT INTO parent VALUES(42)')
        self.db.commit()
        self.assertEqual(self.snapshot(), before)

    def test_successful_full_refresh_replaces_only_site_and_updates_meta(self):
        wp = FakeWP([item('https://example.test/new', 'new content'),
                     item('https://example.test/page', 'page content', '2024-02-01T00:00:00', 4, 'pages')])
        with patch.object(links, 'days_since', return_value=8), patch.object(links, 'now_iso', return_value='new full time'):
            self.assertEqual(links.refresh_corpus(self.db, SITE, wp), 2)
        self.assertIsNone(wp.after)
        self.assertEqual(links.corpus(self.db, SITE), {'https://example.test/new': 'new content',
                                                       'https://example.test/page': 'page content'})
        self.assertEqual(links.corpus(self.db, OTHER_SITE), {'https://other.test/page': 'other content'})
        self.assertEqual(store.get_meta(self.db, SITE), {'corpus_full_at': 'new full time',
                                                       'corpus_watermark': NEW_WATERMARK, 'unrelated': 'preserved'})
        self.assertFalse(self.db.in_transaction)

    def test_successful_incremental_refresh_upserts_and_preserves_others(self):
        wp = FakeWP([item('https://example.test/old', 'updated'),
                     item('https://example.test/new', 'new', post_id=5)])
        with patch.object(links, 'days_since', return_value=1):
            self.assertEqual(links.refresh_corpus(self.db, SITE, wp), 2)
        self.assertEqual(wp.after, OLD_WATERMARK)
        self.assertEqual(links.corpus(self.db, SITE), {'https://example.test/old': 'updated',
                                                       'https://example.test/keep': 'keep content',
                                                       'https://example.test/new': 'new'})
        self.assertEqual(links.corpus(self.db, OTHER_SITE), {'https://other.test/page': 'other content'})
        self.assertEqual(store.get_meta(self.db, SITE), {'corpus_full_at': OLD_FULL,
                                                       'corpus_watermark': NEW_WATERMARK, 'unrelated': 'preserved'})
        self.assertFalse(self.db.in_transaction)

    def test_successful_empty_full_refresh_clears_site_only(self):
        with patch.object(links, 'days_since', return_value=8), patch.object(links, 'now_iso', return_value='new full time'):
            self.assertEqual(links.refresh_corpus(self.db, SITE, FakeWP([])), 0)
        self.assertEqual(links.corpus(self.db, SITE), {})
        self.assertEqual(links.corpus(self.db, OTHER_SITE), {'https://other.test/page': 'other content'})
        self.assertEqual(store.get_meta(self.db, SITE)['corpus_watermark'], OLD_WATERMARK)
        self.assertEqual(store.get_meta(self.db, SITE)['corpus_full_at'], 'new full time')


class FakeListWP:
    """A WordPress that can list posts without content (the weekly check) and return chosen posts' content."""
    def __init__(self, posts, fail_contents=False):
        self.posts = posts                     # [(type, id, url, modified, raw)]
        self.fail_contents = fail_contents
        self.asked = []
        self.ok = True

    def listing(self):
        for typ, pid, url, modified, _raw in self.posts:
            yield {'url': url, 'id': pid, 'type': typ, 'modified': modified}

    def contents(self, typ, ids):
        self.asked.append((typ, sorted(ids)))
        if self.fail_contents:
            raise RuntimeError('dropped connection')
        for t, pid, url, modified, raw in self.posts:
            if t == typ and pid in ids:
                yield {'url': url, 'id': pid, 'type': t, 'modified': modified, 'raw': raw}

    def corpus(self, after=None):
        raise AssertionError('a full check with a stored index must not download every post')


class ListingCheckTests(unittest.TestCase):
    """The weekly full check lists posts without content and downloads only the new or changed ones."""
    def setUp(self):
        self.db = sqlite3.connect(':memory:')
        self.db.row_factory = sqlite3.Row
        self.db.executescript(store.SCHEMA)
        for row in (('https://example.test/old', SITE, 1, 'posts', OLD_WATERMARK, zlib.compress(b'old content')),
                    ('https://example.test/keep', SITE, 2, 'pages', OLD_WATERMARK, zlib.compress(b'keep content')),
                    ('https://other.test/page', OTHER_SITE, 3, 'posts', OLD_WATERMARK, zlib.compress(b'other content'))):
            self.db.execute('INSERT INTO link_corpus VALUES(?,?,?,?,?,?)', row)
        store.set_meta(self.db, SITE, corpus_full_at=OLD_FULL, corpus_watermark=OLD_WATERMARK)
        self.addCleanup(self.db.close)

    def refresh(self, wp):
        known = links.stored_index(self.db, SITE)
        with patch.object(links, 'days_since', return_value=8), patch.object(links, 'now_iso', return_value='full now'):
            rows = links.read_corpus(wp, None, known)
            return links.refresh_corpus(self.db, SITE, wp, rows=rows)

    def test_only_new_and_changed_posts_are_downloaded_and_removed_ones_dropped(self):
        wp = FakeListWP([('pages', 2, 'https://example.test/keep', OLD_WATERMARK, 'never read'),
                         ('posts', 5, 'https://example.test/new', NEW_WATERMARK, 'new content')])
        self.assertEqual(self.refresh(wp), 1)
        self.assertEqual(wp.asked, [('posts', [5])])
        self.assertEqual(links.corpus(self.db, SITE), {'https://example.test/keep': 'keep content',
                                                       'https://example.test/new': 'new content'})
        self.assertEqual(links.corpus(self.db, OTHER_SITE), {'https://other.test/page': 'other content'})
        self.assertEqual(store.get_meta(self.db, SITE)['corpus_full_at'], 'full now')

    def test_a_new_date_or_address_is_downloaded_again(self):
        wp = FakeListWP([('posts', 1, 'https://example.test/old', NEW_WATERMARK, 'edited'),
                         ('pages', 2, 'https://example.test/moved', OLD_WATERMARK, 'moved content')])
        self.assertEqual(self.refresh(wp), 2)
        self.assertEqual(sorted(wp.asked), [('pages', [2]), ('posts', [1])])
        self.assertEqual(links.corpus(self.db, SITE), {'https://example.test/old': 'edited',
                                                       'https://example.test/moved': 'moved content'})

    def test_nothing_changed_downloads_nothing(self):
        wp = FakeListWP([('posts', 1, 'https://example.test/old', OLD_WATERMARK, 'x'),
                         ('pages', 2, 'https://example.test/keep', OLD_WATERMARK, 'y')])
        self.assertEqual(self.refresh(wp), 0)
        self.assertEqual(wp.asked, [])
        self.assertEqual(len(links.corpus(self.db, SITE)), 2)
        self.assertEqual(store.get_meta(self.db, SITE)['corpus_full_at'], 'full now')

    def test_a_failed_download_changes_nothing(self):
        before = [tuple(r) for r in self.db.execute('SELECT * FROM link_corpus ORDER BY url')]
        wp = FakeListWP([('posts', 5, 'https://example.test/new', NEW_WATERMARK, 'new')], fail_contents=True)
        with self.assertRaisesRegex(RuntimeError, 'dropped connection'):
            self.refresh(wp)
        self.assertEqual([tuple(r) for r in self.db.execute('SELECT * FROM link_corpus ORDER BY url')], before)
        self.assertEqual(store.get_meta(self.db, SITE)['corpus_full_at'], OLD_FULL)


class RetryTests(unittest.TestCase):
    def test_a_dropped_connection_is_tried_once_more(self):
        import requests
        from engine import wp as wpmod
        c = wpmod.WPClient.__new__(wpmod.WPClient)
        c.base = 'https://example.test'
        calls = []

        class Session:
            def get(self, url, params=None, timeout=None):
                calls.append(url)
                if len(calls) == 1:
                    raise requests.ConnectionError('Remote end closed connection without response')
                return 'answer'
        c.s = Session()
        with patch.object(wpmod.time, 'sleep'):
            self.assertEqual(c._get('posts', {}), 'answer')
        self.assertEqual(len(calls), 2)

    def test_a_site_that_does_not_answer_is_not_tried_with_every_login(self):
        import tempfile
        import requests
        from engine import wp as wpmod
        calls = []

        class Session:
            headers = {}

            def get(self, url, params=None, headers=None, timeout=None):
                calls.append(url)
                raise requests.Timeout('read timed out')
        c = wpmod.WPClient.__new__(wpmod.WPClient)
        c.site, c.base, c.s, c.unreachable = 'example.test', 'https://example.test', Session(), False
        with tempfile.TemporaryDirectory() as d, patch.object(wpmod, '_hints', return_value=Path(d) / 'hints.json'),                 patch.object(wpmod, 'log'):
            self.assertFalse(c._login([('u%d' % i, 'p%d' % i) for i in range(12)]))
        self.assertEqual(len(calls), 1)
        self.assertTrue(c.unreachable)


if __name__ == '__main__':
    unittest.main()
