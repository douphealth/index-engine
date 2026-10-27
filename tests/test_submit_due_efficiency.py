"""Offline differential and query-count tests for submission selection.

Run with --benchmark to time both implementations on 4,000 synthetic URLs.
"""
import datetime as dt
import random
import sqlite3
import statistics
import sys
import time
import unittest
from unittest.mock import patch

from engine import config, submit, triage
from engine.util import parse_ts


def original_due(db, rows, channel):
    """The original per-URL implementation, retained as an independent oracle."""
    out = []
    for r in rows:
        shown = 1 if r['bing_impr'] else 0
        last = db.execute(
            'SELECT submitted_at, lastmod FROM submissions WHERE url=? AND channel=? '
            'AND http_status BETWEEN 200 AND 299 ORDER BY submitted_at DESC, rowid DESC LIMIT 1',
            (r['url'], channel)).fetchone()
        if last is None:
            out.append((0, shown, -triage.value_score(r), r['url'], r['lastmod']))
            continue
        lm_new, lm_old = parse_ts(r['lastmod']), parse_ts(last['lastmod'])
        if lm_new and (lm_old is None or lm_new > lm_old):
            out.append((1, shown, -triage.value_score(r), r['url'], r['lastmod']))
        elif not shown and not r['bing_crawled'] and (submit.days_since(last['submitted_at']) or 0) >= config.RESUBMIT_UNCHANGED_DAYS:
            out.append((2, shown, -triage.value_score(r), r['url'], r['lastmod']))
    out.sort()
    return [(u, lm) for _, _, _, u, lm in out]


def database():
    db = sqlite3.connect(':memory:')
    db.row_factory = sqlite3.Row
    db.executescript('''CREATE TABLE submissions(url TEXT, channel TEXT, submitted_at TEXT,
                                                lastmod TEXT, http_status INT);
                        CREATE INDEX sub_url ON submissions(url, channel);''')
    return db


def row(url, **overrides):
    result = dict(url=url, kind='post', lastmod='2025-01-01', bing_impr=0,
                  bing_crawled=None, bing_clicks=0, gsc_clicks=0, gsc_impr=0, words=500)
    result.update(overrides)
    return result


def synthetic(db, count, seed=100):
    rng = random.Random(seed)
    rows, records = [], []
    for i in range(count):
        url = f'https://example.test/{i:05d}'
        lm = rng.choice([None, 'invalid', '2024-01-01', '2025-01-01', '2026-01-01'])
        rows.append(row(url, lastmod=lm, bing_impr=rng.choice([0, 0, 5]),
                        bing_crawled=rng.choice([None, None, '2025-01-01']),
                        bing_clicks=rng.randrange(4), gsc_clicks=rng.randrange(4),
                        words=rng.randrange(2001)))
        for _ in range(rng.randrange(5)):
            records.append((url, rng.choice(['indexnow', 'indexnow', 'bing']),
                            rng.choice([None, 'bad', '2024-01-01', '2025-01-01', '2026-01-01']),
                            rng.choice([None, 'invalid', '2024-01-01', '2025-01-01', '2026-01-01']),
                            rng.choice([199, 200, 202, 299, 300, 403, None])))
    db.executemany('INSERT INTO submissions VALUES(?,?,?,?,?)', records)
    return rows


def fixed_days_since(value):
    parsed = parse_ts(value)
    if parsed is None:
        return None
    return (dt.datetime(2026, 10, 27, tzinfo=dt.timezone.utc) - parsed).total_seconds() / 86400


class DueEfficiencyTests(unittest.TestCase):
    def setUp(self):
        self.db = database()
        self.addCleanup(self.db.close)
        for target in ('engine.submit.days_since', 'engine.triage.days_since'):
            p = patch(target, fixed_days_since)
            p.start()
            self.addCleanup(p.stop)

    def compare(self, rows, channel):
        expected = original_due(self.db, rows, channel)
        actual = submit.due(self.db, iter(rows), channel)
        self.assertEqual(expected, actual)
        return actual

    def test_tiers_channel_status_timestamps_ties_and_duplicates(self):
        records = [
            ('tie', 'indexnow', '2025-06-01', '2024-01-01', 200),
            ('tie', 'indexnow', '2025-06-01', '2026-01-01', 299),
            ('tie', 'indexnow', '2026-01-01', None, 300),
            ('tie', 'bing', '2026-02-01', None, 200),
            ('changed', 'indexnow', '2026-01-01', '2024-01-01', 202),
            ('old', 'indexnow', '2024-01-01', '2025-01-01', 200),
            ('shown', 'indexnow', '2024-01-01', '2025-01-01', 200),
            ('crawled', 'indexnow', '2024-01-01', '2025-01-01', 200),
            ('badtime', 'indexnow', 'invalid', '2025-01-01', 200),
            ('nulltime', 'indexnow', None, '2025-01-01', 200),
            ('failed', 'indexnow', '2026-01-01', None, 199),
        ]
        self.db.executemany('INSERT INTO submissions VALUES(?,?,?,?,?)', records)
        rows = [row('tie'), row('changed'), row('old'), row('shown', bing_impr=1),
                row('crawled', bing_crawled='2025-01-01'), row('badtime'), row('nulltime'),
                row('failed'), row('new'), row('new')]
        actual = self.compare(rows, 'indexnow')
        self.assertEqual([url for url, _ in actual], ['failed', 'new', 'new', 'changed', 'old', 'tie'])
        self.compare(rows, 'bing')
        self.compare(rows, None)

    def test_seeded_differential(self):
        for seed in range(8):
            rows = synthetic(self.db, 240, seed)
            rows += rows[:15]
            random.Random(seed).shuffle(rows)
            for channel in ('indexnow', 'bing', 'missing'):
                self.compare(rows, channel)
            self.db.execute('DELETE FROM submissions')

    def test_bounded_chunks_duplicates_empty_and_runtime_limit(self):
        rows = synthetic(self.db, 103)
        rows += rows[:20]
        self.db.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, 17)
        expected = original_due(self.db, rows, 'indexnow')
        statements = []
        self.db.set_trace_callback(statements.append)
        self.assertEqual(submit.due(self.db, rows, 'indexnow'), expected)
        self.db.set_trace_callback(None)
        self.assertEqual(len(statements), 7)  # ceil(103 / (17 - 1))
        statements.clear()
        self.db.set_trace_callback(statements.append)
        self.assertEqual(submit.due(self.db, iter(()), 'indexnow'), [])
        self.db.set_trace_callback(None)
        self.assertEqual(statements, [])

    def test_one_variable_limit_quotes_channel(self):
        channel = "index'now\x00Ω"
        rows = [row('a'), row('b')]
        self.db.execute('INSERT INTO submissions VALUES(?,?,?,?,?)', ('a', channel, '2026-01-01', '2025-01-01', 200))
        expected = original_due(self.db, rows, channel)
        self.db.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, 1)
        self.assertEqual(submit.due(self.db, rows, channel), expected)

    def test_rate_threshold_and_sort_order(self):
        rows = [row('not-yet'), row('at-boundary'), row('shown', bing_impr=1),
                row('higher-value', bing_clicks=2), row('lower-value', bing_clicks=1)]
        self.db.executemany('INSERT INTO submissions VALUES(?,?,?,?,?)', [
            ('not-yet', 'bing', '2026-09-27T00:00:01+00:00', '2025-01-01', 200),
            ('at-boundary', 'bing', '2026-09-27T00:00:00+00:00', '2025-01-01', 200),
            ('shown', 'bing', '2026-09-27T00:00:00+00:00', '2025-01-01', 200),
        ])
        with patch.object(config, 'RESUBMIT_UNCHANGED_DAYS', 30):
            result = self.compare(rows, 'bing')
        self.assertEqual([u for u, _ in result], ['higher-value', 'lower-value', 'at-boundary'])

    def test_4000_url_query_count_and_correctness(self):
        rows = synthetic(self.db, 4000)
        statements = []
        self.db.set_trace_callback(statements.append)
        expected = original_due(self.db, rows, 'indexnow')
        self.assertEqual(len(statements), 4000)
        statements.clear()
        actual = submit.due(self.db, rows, 'indexnow')
        self.db.set_trace_callback(None)
        self.assertEqual(actual, expected)
        size = min(900, self.db.getlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER) - 1)
        self.assertEqual(len(statements), (4000 + size - 1) // size)
        self.assertLess(len(statements), 4000)


def benchmark():
    db = database()
    try:
        rows = synthetic(db, 4000)
        with patch('engine.submit.days_since', fixed_days_since), patch('engine.triage.days_since', fixed_days_since):
            expected = original_due(db, rows, 'indexnow')
            actual = submit.due(db, rows, 'indexnow')
            assert actual == expected, 'correctness check failed; timing abandoned'
            times = {'original': [], 'batched': []}
            for i in range(9):
                order = [('original', original_due), ('batched', submit.due)]
                if i % 2:
                    order.reverse()
                for name, fn in order:
                    start = time.perf_counter()
                    result = fn(db, rows, 'indexnow')
                    times[name].append(time.perf_counter() - start)
                    assert result == expected
            print(f'SQLite {sqlite3.sqlite_version}; URLs={len(rows)}; submissions='
                  f'{db.execute("SELECT count(*) FROM submissions").fetchone()[0]}; due={len(expected)}')
            for name, values in times.items():
                print(f'{name}: median={statistics.median(values)*1000:.3f} ms, '
                      f'range={min(values)*1000:.3f}..{max(values)*1000:.3f} ms, n={len(values)}')
            print(f'median ratio original/batched={statistics.median(times["original"])/statistics.median(times["batched"]):.3f}')
    finally:
        db.close()


if __name__ == '__main__':
    if '--benchmark' in sys.argv:
        benchmark()
    else:
        unittest.main()
