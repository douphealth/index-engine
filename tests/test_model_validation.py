import sqlite3
import unittest
from unittest.mock import patch

import numpy as np

from engine import model


class SiteRateValidationTests(unittest.TestCase):
    def test_fold_features_and_predictions_ignore_validation_labels(self):
        sites = np.array(['a', 'a', 'c', 'a', 'a', 'b', 'b', 'd'])
        y = np.array([1, 0, 1, 1, 0, 1, 0, 1])
        folds = np.array([0, 0, 0, 1, 1, 1, 2, 2])
        # Deliberately poisoned input demonstrates that site_rate is rebuilt.
        X = np.zeros((len(y), len(model.NAMES)))
        X[:, 0] = 999
        X[:, 1] = np.arange(len(y)) / 10
        original = X.copy()
        real_fit, real_predict = model.fit, model.predict

        def run(labels):
            captured = []
            def fit_spy(train_x, train_y):
                result = real_fit(train_x, train_y)
                captured.append([train_x.copy(), train_y.copy(), result])
                return result
            def predict_spy(fitted, test_x):
                predictions = real_predict(fitted, test_x)
                captured[-1].extend([test_x.copy(), predictions.copy()])
                return predictions
            with patch.object(model, 'fit', side_effect=fit_spy), patch.object(model, 'predict', side_effect=predict_spy):
                scores = model.cross_validate(X, labels, sites, folds)
            return scores, captured

        scores, first = run(y)
        changed = y.copy()
        changed[folds == 0] = 1 - changed[folds == 0]
        _, second = run(changed)
        self.assertIsNotNone(scores[0])
        train_x, train_y, fitted, test_x, predictions = first[0]
        np.testing.assert_allclose(train_x[:, 0], [1/5, 2/5, 1/5, 2/5, .2])
        np.testing.assert_allclose(test_x[:, 0], [2/6, 2/6, .2])
        np.testing.assert_array_equal(train_y, y[folds != 0])
        for i in (0, 1, 3, 4):
            np.testing.assert_allclose(first[0][i], second[0][i])
        for key in ('w', 'mu', 'sd'):
            np.testing.assert_allclose(fitted[key], second[0][2][key])
        np.testing.assert_array_equal(X, original)

    def test_every_fold_excludes_its_validation_labels(self):
        sites = np.array(['a', 'a', 'a', 'b', 'b', 'b', 'c', 'c', 'c'])
        y = np.array([1, 0, 1, 0, 1, 0, 1, 0, 1])
        folds = np.tile([0, 1, 2], 3)
        X = np.full((len(y), len(model.NAMES)), 44.0)
        calls = []
        real_fit, real_predict = model.fit, model.predict
        def fit_spy(x, labels):
            calls.append([x.copy(), labels.copy()])
            return real_fit(x, labels)
        def predict_spy(m, x):
            calls[-1].append(x.copy())
            return real_predict(m, x)
        with patch.object(model, 'fit', side_effect=fit_spy), patch.object(model, 'predict', side_effect=predict_spy):
            model.cross_validate(X, y, sites, folds)
        self.assertEqual(len(calls), 3)
        for fold, (train_x, train_y, test_x) in enumerate(calls):
            train = folds != fold
            for j, site in enumerate(sites[train]):
                others = (sites[train] == site)
                n = int(others.sum()) - 1
                positives = int(train_y[others].sum()) - int(train_y[j])
                self.assertAlmostEqual(train_x[j, 0], (positives + 1)/(n + 4) if n else .2)
            for j, site in enumerate(sites[~train]):
                same = sites[train] == site
                n = int(same.sum())
                self.assertAlmostEqual(test_x[j, 0], (int(train_y[same].sum()) + 1)/(n + 4) if n else .2)

    def test_frame_excludes_own_label_but_inference_uses_all_known_labels(self):
        def make_frame(first_bucket):
            db = sqlite3.connect(':memory:')
            db.row_factory = sqlite3.Row
            db.executescript('''
                CREATE TABLE triage(url, site, bucket, flags, value);
                CREATE TABLE urls(url, in_sitemap, kind, published, first_seen, lastmod);
                CREATE TABLE audits(url, words, title, status, hops);
                CREATE TABLE perf(url, bing_impr, bing_clicks);
                CREATE TABLE gsc(url, last_crawl);
                CREATE TABLE links(src, dst, in_content);
            ''')
            data = [('p', 'a', first_bucket), ('n', 'a', 'CRAWLED_NOT_INDEXED'),
                    ('u', 'a', 'NOT_INSPECTED'), ('s', 'b', 'INDEXED'),
                    ('v', 'b', 'GOOGLE_STALE'), ('z', 'unseen', 'NOT_INSPECTED')]
            for url, site, bucket in data:
                db.execute('INSERT INTO triage VALUES (?, ?, ?, ?, ?)', (url, site, bucket, '', 1))
                db.execute('INSERT INTO urls VALUES (?, 0, ?, NULL, NULL, NULL)', (url, 'post'))
            return {r['url']: r['raw']['site_rate'] for r in model.frame(db)}
        positive = make_frame('INDEXED')
        negative = make_frame('CRAWLED_NOT_INDEXED')
        self.assertAlmostEqual(positive['p'], 1/5)
        self.assertEqual(positive['p'], negative['p'])
        self.assertAlmostEqual(positive['n'], 2/5)
        self.assertAlmostEqual(positive['u'], 2/6)
        self.assertAlmostEqual(negative['u'], 1/6)
        self.assertEqual(positive['s'], .2)
        self.assertAlmostEqual(positive['v'], 2/5)
        self.assertEqual(positive['z'], .2)

    def test_final_fit_rebuilds_leave_one_out_even_for_poisoned_records(self):
        raw = dict(site_rate=999, inl_indexed=0, inl_body=0, inl_all=0, words=0,
                   near_dup=0, thin=0, trust=0, bing_impr=0, bing_click=0,
                   age=0, depth=0, in_sitemap=0, is_post=0)
        records = [dict(url=str(i), site='a' if i < 39 else 'singleton',
                        bucket='INDEXED' if i < 20 else 'CRAWLED_NOT_INDEXED', raw=raw.copy())
                   for i in range(40)]
        captured = []
        real_fit = model.fit
        def fit_spy(x, y):
            captured.append((x.copy(), y.copy()))
            return real_fit(x, y)
        with patch.object(model, 'cross_validate', return_value=(.7, None)), patch.object(model, 'fit', side_effect=fit_spy):
            fitted, meta = model.train(records)
        self.assertIsNotNone(fitted)
        self.assertEqual(meta['quality'], 'fair')
        x, y = captured[0]
        np.testing.assert_allclose(x[:20, 0], 20/42)
        np.testing.assert_allclose(x[20:39, 0], 21/42)
        self.assertEqual(x[39, 0], .2)
        self.assertEqual(sum(y), 20)
        self.assertEqual(records[0]['raw']['site_rate'], 999)

    def test_small_or_invalid_fold_samples_are_insufficient(self):
        m, meta = model.train([{'bucket': 'INDEXED'}, {'bucket': 'CRAWLED_NOT_INDEXED'}])
        self.assertIsNone(m)
        self.assertEqual(meta['quality'], 'insufficient')
        X = np.zeros((4, len(model.NAMES)))
        y = np.array([1, 1, 0, 0])
        sites = np.array(['a'] * 4)
        self.assertEqual(model.cross_validate(X, y, sites, np.zeros(4)), (None, None))
        self.assertEqual(model.cross_validate(X, y, sites, np.array([0, 0, 1, 1])), (None, None))


if __name__ == '__main__':
    unittest.main()
