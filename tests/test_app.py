import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import app


class SearchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.original = app.DB
        app.DB = Path(self.temp.name) / 'test.db'
        app.init_db()

    def tearDown(self):
        app.DB = self.original
        self.temp.cleanup()

    def test_japanese_and_short_queries(self):
        app.save_page('https://example.com/', '京都の旅', '京都の観光と日本の文化を紹介します')
        self.assertEqual(len(app.search('京都')), 1)
        self.assertEqual(len(app.search('日本の文化')), 1)
        self.assertEqual(len(app.search('京都 文化')), 1)
        self.assertEqual(app.search('京都 東京'), [])
        self.assertEqual(app.search(''), [])

    def test_upsert_removes_old_index(self):
        app.save_page('https://example.com/', 'Old title', 'obsolete')
        app.save_page('https://example.com/', 'New title', 'replacement')
        self.assertEqual(app.search('obsolete'), [])
        self.assertEqual(len(app.search('replacement')), 1)

    def test_queries_are_literal(self):
        self.assertEqual(app.search('" OR *'), [])

    def test_html_extraction(self):
        page = app.Page()
        page.feed('<title>Hello &amp; world</title><script>secret</script><p>Visible</p><a href="/next">Next</a>')
        self.assertEqual(''.join(page.title), 'Hello & world')
        self.assertNotIn('secret', page.text)
        self.assertEqual(page.links, ['/next'])

    def test_private_addresses_blocked(self):
        for address in ['127.0.0.1', '10.0.0.1', '169.254.169.254', '::1']:
            with patch('socket.getaddrinfo', return_value=[(2, 1, 6, '', (address, 80))]):
                with self.assertRaises(ValueError):
                    app.public_url('http://example.com/')
        with self.assertRaises(ValueError):
            app.public_url('file:///etc/passwd')


if __name__ == '__main__':
    unittest.main()
