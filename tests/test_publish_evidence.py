"""Evidence dates must be the original observations, never the upload time."""
import tempfile
import unittest
from pathlib import Path
from engine import publish, store


class PublishEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = store.connect(Path(self.tmp.name) / 'state.db')
        self.site, self.url = 'example.com', 'https://example.com/page/'
        self.db.execute('INSERT INTO urls(url,site,kind,in_sitemap) VALUES(?,?,?,?)', (self.url, self.site, 'post', 1))
        self.db.execute('INSERT INTO triage(url,site,bucket,value) VALUES(?,?,?,?)', (self.url, self.site, 'INDEXED', 10))
        self.results = {self.site: {'meta': {}, 'snapshot': {'total': 1, 'bing_earners': 0}, 'counts': {'INDEXED': 1},
                                  'diagnosis': [], 'plan': [], 'dups': {}, 'manual': []}}

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def exported(self):
        _, tables, _ = publish.build(self.db, self.results, '2026-10-30T12:00:00Z')
        table = tables[self.site]
        return dict(zip(table['cols'], table['rows'][0]))

    def test_missing_evidence_remains_missing_even_when_bucket_is_indexed(self):
        row = self.exported()
        self.assertEqual(row['bucket'], 'INDEXED')
        for key in ('checked_at', 'inspected_at', 'google_verdict', 'gsc_clicks', 'bing_clicks', 'linked_at'):
            self.assertEqual(row[key], '')

    def test_upload_does_not_replace_observation_dates(self):
        self.db.execute('INSERT INTO audits(url,checked_at,status,canonical,robots_google) VALUES(?,?,?,?,?)',
                        (self.url, '2026-10-20T08:00:00Z', 200, self.url, 1))
        self.db.execute('INSERT INTO gsc(url,inspected_at,verdict,coverage,error) VALUES(?,?,?,?,?)',
                        (self.url, '2026-10-21T09:00:00Z', 'PASS', 'Submitted and indexed', 'HTTP 503'))
        row = self.exported()
        self.assertEqual(row['checked_at'], '2026-10-20T08:00:00Z')
        self.assertEqual(row['inspected_at'], '2026-10-21T09:00:00Z')
        self.assertEqual(row['google_error'], 'HTTP 503')
        self.assertEqual(row['http_status'], 200)

    def test_unconnected_source_is_not_reported_as_zero_clicks(self):
        self.db.execute('INSERT INTO perf(url,site,bing_clicks,bing_at) VALUES(?,?,?,?)',
                        (self.url, self.site, 7, '2026-10-28T08:00:00Z'))
        row = self.exported()
        self.assertEqual(row['bing_clicks'], 7)
        self.assertEqual(row['gsc_clicks'], '')

    def test_latest_applied_link_excludes_undone_links(self):
        for id_, status, date in [('a', 'applied', '2026-10-22'), ('b', 'applied', '2026-10-23'), ('c', 'undone', '2026-10-24')]:
            self.db.execute('INSERT INTO link_ops(id,target,status,applied_at) VALUES(?,?,?,?)', (id_, self.url, status, date))
        self.assertEqual(self.exported()['linked_at'], '2026-10-23')

    def test_legacy_column_order_is_unchanged(self):
        self.assertEqual(publish.COLS[:15], ['url', 'bucket', 'flags', 'reason', 'action', 'value', 'google', 'bing',
                                          'kind', 'in_sitemap', 'words', 'title', 'odds', 'why', 'trust'])


if __name__ == '__main__':
    unittest.main()
