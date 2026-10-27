import random
import unittest

from engine.links import GAP, candidates, segments, tokens


def original_candidates(raw, keys):
    """Reference implementation for differential tests."""
    found = {}
    for s, e, ok in segments(raw):
        if not ok:
            continue
        toks = tokens(raw[s:e])
        for n in (5, 4, 3, 2):
            if len(toks) < n + 3:
                continue
            for i in range(len(toks) - n + 1):
                key = tuple(t for t, _, _ in toks[i:i + n])
                if key not in keys or key in found:
                    continue
                if all(GAP.match(raw[s + toks[j][2]:s + toks[j + 1][1]]) for j in range(i, i + n - 1)):
                    found[key] = (s + toks[i][1], s + toks[i + n - 1][2])
    return found


class CountingKeys(set):
    def __init__(self, values):
        super().__init__(values)
        self.lookups = 0
        self.iterations = 0

    def __iter__(self):
        self.iterations += 1
        return super().__iter__()

    def __contains__(self, item):
        self.lookups += 1
        return super().__contains__(item)


class CandidateTests(unittest.TestCase):
    def test_prepared_prefixes_are_reused_across_multiple_pages(self):
        from engine import links
        keys = CountingKeys({('special', 'running', 'shoe')})
        starts = links.candidate_starts(keys)
        for _ in range(10):
            raw = '<p>These special running shoes help the athletes run comfortably today.</p>'
            self.assertEqual(links.candidates(raw, keys, starts), original_candidates(raw, keys))
        self.assertEqual(keys.iterations, 1, 'prefixes should be built once, not once per page')

    def test_skips_full_phrase_lookups_for_impossible_first_words(self):
        keys = CountingKeys({('specific', 'target')})
        raw = '<p>' + 'ordinary unrelated words appear throughout this long passage ' * 100 + '</p>'
        self.assertEqual(candidates(raw, keys), {})
        self.assertLess(keys.lookups, 10, 'unrelated text should not require a lookup for every n-gram')

    def test_finds_first_safe_occurrence_with_offsets_and_entities(self):
        raw = '<h2>Special Running Shoes</h2><p>These special running shoes help the athletes run comfortably today.</p><p>More special running shoes are here and ready for the runners.</p>'
        keys = {('special', 'running', 'shoe')}
        result = candidates(raw, keys)
        self.assertEqual(raw[slice(*result[('special', 'running', 'shoe')])], 'special running shoes')
        self.assertEqual(result[('special', 'running', 'shoe')][0], raw.index('special running shoes'))

    def test_matches_original_on_seeded_mixed_html(self):
        rng = random.Random(42)
        vocab = ['special', 'running', 'shoes', 'dog', 'care', 'daily', 'health', 'young', 'runner']
        separators = [' ', '-', ', ', '&nbsp;', '. ']
        for _ in range(100):
            text = ''.join(rng.choice(vocab) + rng.choice(separators) for _ in range(80))
            raw = '<h2>' + text[:60] + '</h2><p>' + text + '</p><a href="/">' + text[:90] + '</a>'
            ts = [word for word, _, _ in tokens(text)]
            keys = {tuple(ts[i:i + n]) for n in (2, 3, 4, 5) for i in range(0, len(ts) - n + 1, 7)}
            self.assertEqual(candidates(raw, keys), original_candidates(raw, keys))

    def test_does_not_match_across_punctuation(self):
        raw = '<p>These special, running shoes help the athletes run comfortably today.</p>'
        self.assertEqual(candidates(raw, {('special', 'running', 'shoe')}), {})


if __name__ == '__main__':
    unittest.main()
