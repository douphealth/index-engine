import tempfile
import unittest
from pathlib import Path

from engine import store


class LinkOperationIndexTests(unittest.TestCase):
    def test_latest_applied_link_uses_target_index_on_existing_database(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'existing.db'
            db = store.connect(path)
            db.executemany('INSERT INTO link_ops(id,target,status,applied_at) VALUES(?,?,?,?)', [
                ('1', 'https://example.test/a', 'applied', '2026-01-01'),
                ('2', 'https://example.test/a', 'applied', '2026-02-01'),
                ('3', 'https://example.test/a', 'undone', '2026-03-01'),
                ('4', 'https://example.test/b', 'applied', '2026-04-01'),
            ])
            # Simulate a database created before the covering index existed.
            db.execute('DROP INDEX IF EXISTS link_ops_target_status_at')
            db.commit()
            db.close()
            db = store.connect(path)
            try:
                query = "SELECT MAX(applied_at) FROM link_ops WHERE target=? AND status='applied'"
                self.assertEqual(db.execute(query, ('https://example.test/a',)).fetchone()[0], '2026-02-01')
                self.assertIsNone(db.execute(query, ('https://example.test/missing',)).fetchone()[0])
                plan = ' '.join(row[3] for row in db.execute('EXPLAIN QUERY PLAN ' + query, ('https://example.test/a',)))
                self.assertIn('link_ops_target_status_at', plan)
            finally:
                db.close()


if __name__ == '__main__':
    unittest.main()
