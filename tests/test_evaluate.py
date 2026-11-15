import json
import tempfile
import unittest
from pathlib import Path
import app
import evaluate


class EvaluationTests(unittest.TestCase):
    def test_missing_pages_count_as_misses(self):
        with tempfile.TemporaryDirectory() as folder:
            original = app.DB
            try:
                app.DB = Path(folder) / 'test.db'
                app.init_db()
                app.save_page('https://example.com/', 'Python', 'Python tutorial')
                cases = [
                    {'id': 'hit', 'query': 'Python', 'expected_urls': ['https://example.com/']},
                    {'id': 'missing', 'query': 'missing', 'expected_urls': ['https://absent.test/']},
                ]
                report = evaluate.evaluate(cases)
                self.assertEqual((report['covered'], report['hits_at_5']), (1, 1))
                self.assertEqual(report['hit_rate'], 0.5)
                self.assertFalse(report['quality_pass'])
                self.assertFalse(report['scale_speed_pass'])
                self.assertEqual(report['results'][0]['rank'], 1)
                self.assertIsNone(report['results'][1]['rank'])
            finally:
                app.DB = original

    def test_suite_has_twenty_tasks_across_five_sites(self):
        suite = json.loads(evaluate.CASES.read_text())
        self.assertEqual(len(suite['sites']), 5)
        self.assertEqual(len(suite['cases']), 20)
        self.assertEqual(len({case['id'] for case in suite['cases']}), 20)
        for site in suite['sites']:
            self.assertEqual(sum(case['site'] == site['id'] for case in suite['cases']), 4)
