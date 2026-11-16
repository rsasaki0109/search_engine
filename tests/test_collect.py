import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import app
import collect


class CollectionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.original = app.DB
        app.DB = Path(self.temp.name) / 'test.db'
        app.init_db()
        collect.init_frontier()

    def tearDown(self):
        app.DB = self.original
        self.temp.cleanup()

    def test_scope_excludes_other_hosts_paths_queries_and_files(self):
        seed = 'https://example.com/docs'
        for url in ['https://other.test/docs/a', 'https://example.com/docs-old/a',
                    'https://example.com/docs/a?lang=en', 'https://example.com/docs/a.pdf',
                    'file:///docs/a']:
            self.assertIsNone(collect.scoped_url(url, seed, '/docs'))
        self.assertEqual(collect.scoped_url(seed + '/a#section', seed, '/docs'), seed + '/a')

    def test_persistent_frontier_can_resume_after_budget(self):
        site = {'id': 'git', 'seed': 'https://example.com/docs'}
        def fetch(url):
            if url.endswith('/robots.txt'):
                return 'User-agent: *\nDisallow: /docs/blocked', 'text/plain', url
            return '<title>Document</title><a href="/docs/next">Next</a><a href="/docs/blocked">Blocked</a><a href="/outside">Out</a>', 'text/html', url
        with patch.object(app, 'fetch', side_effect=fetch), patch.object(collect.time, 'sleep'):
            first = collect.collect_site(site, target=2, budget=1)
            self.assertEqual(first['pages'], 1)
            self.assertGreater(first['frontier']['pending'], 0)
            second = collect.collect_site(site, target=2, budget=10)
            self.assertEqual(second['pages'], 2)
        with app.connect() as db:
            self.assertEqual(db.execute("SELECT status FROM frontier WHERE url LIKE '%blocked'").fetchone()[0], 'skipped')
            self.assertEqual(db.execute("SELECT count(*) FROM frontier WHERE url LIKE '%outside'").fetchone()[0], 0)

    def test_failed_requests_are_saved_and_interrupted_requests_recover(self):
        def fetch(url):
            if url.endswith('/robots.txt'):
                return '', 'text/plain', url
            raise ValueError('fetch failed')
        with patch.object(app, 'fetch', side_effect=fetch), patch.object(collect.time, 'sleep'):
            result = collect.collect_site({'id': 'git', 'seed': 'https://example.com/docs'}, target=1)
        self.assertEqual(result['frontier']['failed'], 1)
        with app.connect() as db:
            self.assertEqual(db.execute('SELECT reason FROM frontier').fetchone()[0], 'fetch failed')
            db.execute("UPDATE frontier SET status='fetching'")
        collect.init_frontier()
        with app.connect() as db:
            self.assertEqual(db.execute('SELECT status FROM frontier').fetchone()[0], 'pending')
