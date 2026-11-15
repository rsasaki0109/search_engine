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

    def test_snippet_finds_match_beyond_start(self):
        app.save_page('https://example.com/', 'Document', '前置き' * 300 + 'Python tutorial' + 'あとがき' * 300)
        result = app.search('python')[0]
        self.assertIn('Python', result['excerpt'])
        self.assertTrue(result['excerpt'].startswith('…'))
        self.assertLessEqual(len(result['excerpt']), 262)

    def test_pagination_counts_and_bounds(self):
        for i in range(23):
            app.save_page(f'https://example.com/{i:02}', '検索 Search', '検索できる本文')
        for query in ['検索', 'Search']:
            first = app.search_page(query)
            second = app.search_page(query, 2)
            last = app.search_page(query, 999)
            self.assertEqual((first['total'], first['pages']), (23, 3))
            self.assertEqual(len(first['results']), 10)
            self.assertTrue(set(r['url'] for r in first['results']).isdisjoint(r['url'] for r in second['results']))
            self.assertEqual((last['page'], len(last['results'])), (3, 3))
            self.assertEqual(app.search_page(query, -1)['page'], 1)
        self.assertEqual(app.search_page('missing')['pages'], 0)

    def test_library_filter_pagination_and_delete(self):
        for i in range(12):
            app.save_page(f'https://example.com/{i:02}', f'文書 {i}', 'searchable text')
        self.assertEqual(app.list_pages()['total'], 12)
        self.assertEqual(len(app.list_pages(page=2)['results']), 2)
        self.assertEqual(app.list_pages('文書 11')['total'], 1)
        self.assertEqual(app.list_pages('/00')['total'], 1)
        self.assertTrue(app.delete_page('https://example.com/00'))
        self.assertFalse(app.delete_page('https://example.com/00'))
        self.assertEqual(app.list_pages()['total'], 11)
        self.assertEqual(len(app.search('searchable')), 11)
        self.assertEqual(app.list_pages('/00')['results'], [])

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
