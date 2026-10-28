"""The trust scan must catch the invented-expert patterns the first version missed, and must leave real citations alone."""
import unittest

from engine import audit


def kinds(text):
    return {h["kind"] for h in audit.trust_scan(text)}


class TrustScanTests(unittest.TestCase):
    def test_quote_credited_to_a_named_expert_without_a_year(self):
        self.assertIn("named expert quote", kinds("great advice here. — Dr. Layne Norton, PhD, 2025 ISSN Conference Keynote"))
        self.assertIn("named expert quote", kinds("some words —Dr. Lena Ortiz, Sports Physiotherapist, ACSM"))
        self.assertIn("named expert quote", kinds("tips. — Data from a 2025 ISSN Conference workshop by Dr. Eric Helms"))

    def test_expert_named_in_a_sentence_or_in_brackets(self):
        self.assertIn("named expert says", kinds("It is renoprotective, says Dr. Elena Torres, nephrologist at Mayo Clinic."))
        self.assertIn("named expert in brackets", kinds("Alternate running and walking for joint health (Dr. Jane Smith, ACSM trainer)."))

    def test_invented_bylines_experiments_and_panels(self):
        self.assertIn("author byline to verify", kinds("Written by: Dr. Alex Rivera, Certified Primal Fitness Expert"))
        self.assertIn("placeholder name", kinds("By John Doe, NASM-certified trainer, featured in a magazine."))
        self.assertIn("invented experiment", kinds("So in January I split 68 volunteers into two groups."))
        self.assertIn("claimed testing panel", kinds("Our panel of dietitians and sensory experts blind-tested every option."))
        self.assertIn("tested everything", kinds("We tested every model we could find."))

    def test_real_citations_and_plain_mentions_are_not_flagged(self):
        for ok in (
            "In 1957, Finnish physiologist Dr. Martti Karvonen proved that percentage of heart rate reserve correlates with VO2 reserve.",
            "This guide draws on elite programming from coaches like Dr. Layne Norton and Dr. Mike Israetel, and practical cues.",
            "This is the principle behind Dr. Mike Israetel's RP Strength models.",
            "Exercise physiologists such as Dr. Stephen Seiler and Dr. Inigo San Millan divide intensity into three zones.",
            "Our team wrote this guide. The lab results below come from the cited paper.",
        ):
            self.assertEqual(kinds(ok), set(), ok)

    def test_the_old_patterns_still_work(self):
        self.assertIn("treated/tested N", kinds("We tested 150 miles."))
        self.assertIn("own clients/patients", kinds("I tell my clients to stretch."))
        self.assertIn("first-hand practice", kinds("In my practice, results vary."))
        self.assertIn("named doctor + year", kinds("Research by Dr. Stephen Seiler (2010) showed a pattern."))

    def test_one_hit_per_span_and_a_limit(self):
        hits = audit.trust_scan("— Dr. Lena Torres, 2025 Report")        # matches two patterns on the same words
        self.assertEqual(len(hits), 1)
        many = " ".join(f"We tested {n}00 shoes." for n in range(1, 30))
        self.assertEqual(len(audit.trust_scan(many)), 8)


class PageWeightTests(unittest.TestCase):
    def test_pasted_base64_images_are_counted_with_or_without_the_data_prefix(self):
        payload = "A" * 2500
        html = f'<img src="data:image/jpeg;base64,{payload}"><img src="image/png;base64,{payload}">'
        self.assertEqual(sum(len(m) for m in audit.INLINE_B64.findall(html)), 5000)

    def test_tiny_inline_icons_are_ignored(self):
        self.assertEqual(audit.INLINE_B64.findall('<img src="data:image/gif;base64,R0lGODlhAQABAAAAACw=">'), [])


if __name__ == "__main__":
    unittest.main()
