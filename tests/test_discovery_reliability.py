import sqlite3
import unittest
import xml.etree.ElementTree as ET
from unittest.mock import patch

from engine import discover, store

SITE = 'example.test'
BASE = 'https://' + SITE
ROOT = BASE + '/sitemap.xml'
NS = 'http://www.sitemaps.org/schemas/sitemap/0.9'


class Response:
    def __init__(self, body='', status=200, items=None, total=None, content_type='application/json'):
        self.status_code = status
        self.text = body
        self.content = body.encode()
        self.items = items
        self.headers = {'Content-Type': content_type}
        if total is not None:
            self.headers['X-WP-TotalPages'] = str(total)

    def json(self):
        if isinstance(self.items, Exception):
            raise self.items
        return self.items


def urlset(*urls):
    return f'<urlset xmlns="{NS}">' + ''.join(f'<url><loc>{url}</loc></url>' for url in urls) + '</urlset>'


def index(*urls):
    return f'<sitemapindex xmlns="{NS}">' + ''.join(f'<sitemap><loc>{url}</loc></sitemap>' for url in urls) + '</sitemapindex>'


def wp(*urls, total=1):
    return Response(items=[{'link': url} for url in urls], total=total)


class DiscoveryReliabilityTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(':memory:')
        self.db.row_factory = sqlite3.Row
        self.db.executescript(store.SCHEMA)
        for migration in store.MIGRATIONS:
            self.db.execute(migration)
        self.addCleanup(self.db.close)
        self.old = BASE + '/old/'
        self.new = BASE + '/new/'
        self.db.execute('INSERT INTO urls(url, site, in_sitemap, in_wp) VALUES (?, ?, 1, 1)', (self.old, SITE))
        self.db.commit()

    def scan(self, responses, use_wp=True, robots=True):
        def get(url, params=None, **kwargs):
            if url == BASE + '/robots.txt':
                return Response('Sitemap: ' + ROOT if robots else '')
            key = (url, params['page']) if params else url
            response = responses.get(key)
            if response is None:
                raise AssertionError(f'unexpected request: {key}')
            if isinstance(response, Exception):
                raise response
            return response
        with patch.object(discover, 'get', side_effect=get):
            discover.run(self.db, [SITE], use_wp=use_wp)

    def members(self, column):
        return {r['url'] for r in self.db.execute(f'SELECT url FROM urls WHERE site=? AND {column}=1', (SITE,))}

    def test_child_failure_preserves_sitemap_and_adds_partial_results(self):
        good, bad = BASE + '/good.xml', BASE + '/bad.xml'
        self.scan({ROOT: Response(index(good, bad)), good: Response(urlset(self.new)), bad: OSError('failed')}, use_wp=False)
        self.assertEqual(self.members('in_sitemap'), {self.old, self.new})
        meta = store.get_meta(self.db, SITE)
        self.assertFalse(meta['sitemap_complete'])
        self.assertEqual(meta['sitemap_count'], 2)
        self.assertTrue(meta['sitemap_errors'])
        self.assertEqual(self.members('in_wp'), {self.old})

    def test_later_wp_page_failure_preserves_wp_but_replaces_sitemap(self):
        self.scan({ROOT: Response(urlset(self.new)),
                   (BASE + '/wp-json/wp/v2/posts', 1): wp(self.new, total=2),
                   (BASE + '/wp-json/wp/v2/posts', 2): Response(status=503)})
        self.assertEqual(self.members('in_wp'), {self.old, self.new})
        self.assertEqual(self.members('in_sitemap'), {self.new})
        meta = store.get_meta(self.db, SITE)
        self.assertFalse(meta['wp_complete'])
        self.assertEqual(meta['wp_count'], 2)
        self.assertEqual(meta['missing_from_sitemap'], 1)

    def test_failed_sitemap_does_not_block_successful_wp_replacement(self):
        child = BASE + '/broken.xml'
        self.scan({ROOT: Response(index(child)), child: OSError('failed'),
                   (BASE + '/wp-json/wp/v2/posts', 1): wp(self.new),
                   (BASE + '/wp-json/wp/v2/pages', 1): wp(total=0)})
        self.assertEqual(self.members('in_sitemap'), {self.old})
        self.assertEqual(self.members('in_wp'), {self.new})

    def test_successful_scans_replace_both_memberships(self):
        self.scan({ROOT: Response(urlset(self.new)),
                   (BASE + '/wp-json/wp/v2/posts', 1): wp(self.new),
                   (BASE + '/wp-json/wp/v2/pages', 1): wp(total=0)})
        self.assertEqual(self.members('in_sitemap'), {self.new})
        self.assertEqual(self.members('in_wp'), {self.new})
        self.assertTrue(store.get_meta(self.db, SITE)['wp_complete'])

    def test_successful_empty_sources_replace_with_empty(self):
        self.scan({ROOT: Response(urlset()),
                   (BASE + '/wp-json/wp/v2/posts', 1): wp(total=0),
                   (BASE + '/wp-json/wp/v2/pages', 1): wp(total=0)})
        self.assertEqual(self.members('in_sitemap'), set())
        self.assertEqual(self.members('in_wp'), set())
        meta = store.get_meta(self.db, SITE)
        self.assertTrue(meta['sitemap_complete'])
        self.assertTrue(meta['wp_complete'])
        self.assertEqual((meta['sitemap_count'], meta['wp_count']), (0, 0))

    def test_first_wp_collection_empty_still_reads_pages(self):
        self.scan({ROOT: Response(urlset()),
                   (BASE + '/wp-json/wp/v2/posts', 1): wp(total=0),
                   (BASE + '/wp-json/wp/v2/pages', 1): wp(self.new)})
        self.assertEqual(self.members('in_wp'), {self.new})

    def test_no_sitemap_roots_and_failed_wp_preserve_inventory(self):
        self.scan({(BASE + '/wp-json/wp/v2/posts', 1): Response(status=503)}, robots=False)
        self.assertEqual(self.members('in_sitemap'), {self.old})
        self.assertEqual(self.members('in_wp'), {self.old})
        meta = store.get_meta(self.db, SITE)
        self.assertFalse(meta['sitemap_complete'])
        self.assertFalse(meta['wp_complete'])

    def test_wp_status_survives_incomplete_or_disabled_scan(self):
        store.set_meta(self.db, SITE, wp=True)
        self.scan({ROOT: Response(urlset(self.old)),
                   (BASE + '/wp-json/wp/v2/posts', 1): Response(status=503)})
        self.assertTrue(store.get_meta(self.db, SITE)['wp'])
        self.assertFalse(store.get_meta(self.db, SITE)['wp_complete'])
        self.scan({ROOT: Response(urlset(self.old))}, use_wp=False)
        self.assertTrue(store.get_meta(self.db, SITE)['wp'])

    def test_empty_wp_page_before_declared_end_is_incomplete(self):
        self.scan({ROOT: Response(urlset(self.new)),
                   (BASE + '/wp-json/wp/v2/posts', 1): wp(self.new, total=3),
                   (BASE + '/wp-json/wp/v2/posts', 2): wp(total=3)})
        self.assertEqual(self.members('in_wp'), {self.old, self.new})
        self.assertFalse(store.get_meta(self.db, SITE)['wp_complete'])

    def test_sitemap_cap_records_incomplete(self):
        child = BASE + '/child.xml'
        with patch.object(discover, '_xml', return_value=ET.fromstring(index(child))):
            found, read, errors = discover.walk_sitemaps(BASE, [ROOT], SITE, cap=1)
        self.assertEqual(read, [ROOT])
        self.assertFalse(found)
        self.assertTrue(errors)

    def test_wp_page_cap_and_bad_page_count_are_incomplete(self):
        for response in (wp(self.new, total=2), wp(self.new, total='bad')):
            with self.subTest(total=response.headers['X-WP-TotalPages']):
                with patch.object(discover, 'get', return_value=response):
                    found, complete = discover._wp_published_scan(BASE, SITE, max_pages=1)
                self.assertFalse(complete)
                self.assertIn(self.new, found)

    def test_unexpected_sitemap_xml_does_not_clear(self):
        self.scan({ROOT: Response('<html/>')}, use_wp=False)
        self.assertEqual(self.members('in_sitemap'), {self.old})
        self.assertFalse(store.get_meta(self.db, SITE)['sitemap_complete'])


if __name__ == '__main__':
    unittest.main()
