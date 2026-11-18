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
        app.cancel_event.clear()

    def tearDown(self):
        app.cancel_event.clear()
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

    def test_crawl_history_records_outcomes_and_survives_restart(self):
        fixtures = {
            'https://example.com/robots.txt': ('User-agent: *\nDisallow: /blocked', 'text/plain', 'https://example.com/robots.txt'),
            'https://example.com/': ('<title>Home</title><a href="/blocked">blocked</a><a href="/pdf">PDF</a><a href="/error">error</a>', 'text/html', 'https://example.com/'),
            'https://example.com/pdf': ('data', 'application/pdf', 'https://example.com/pdf'),
        }
        def fetch(url):
            if url.endswith('/error'):
                raise ValueError('テスト用の取得失敗')
            return fixtures[url]
        app.crawl_lock.acquire()
        with patch.object(app, 'public_url', side_effect=lambda url: url), patch.object(app, 'fetch', side_effect=fetch), patch.object(app.cancel_event, 'wait', return_value=False):
            app.crawl('https://example.com/', 10)
        self.assertFalse(app.crawl_lock.locked())
        run = app.crawl_history()[0]
        self.assertEqual(run['status'], 'partial')
        self.assertEqual(run['indexed'], 1)
        self.assertEqual([e['status'] for e in run['events']], ['success', 'skipped', 'skipped', 'failed'])
        self.assertIn('取得失敗', run['events'][-1]['reason'])
        app.init_db()
        self.assertEqual(app.crawl_history()[0], run)

    def test_crawl_failure_and_interrupted_history(self):
        app.crawl_lock.acquire()
        with patch.object(app, 'public_url', side_effect=ValueError('URL拒否')):
            app.crawl('http://localhost/', 1)
        self.assertEqual(app.crawl_history()[0]['status'], 'failed')
        self.assertIn('URL拒否', app.crawl_history()[0]['message'])
        with app.connect() as db:
            db.execute("INSERT INTO crawls(seed,started,status,indexed,message,events) VALUES ('https://example.com',?,'running',0,'','[]')", (app.utc_now(),))
        app.init_db()
        self.assertEqual(app.crawl_history()[0]['status'], 'interrupted')
        self.assertTrue(app.crawl_history()[0]['finished'])

    def test_cancel_preserves_page_and_does_not_fetch_next(self):
        fetched = []
        def fetch(url):
            fetched.append(url)
            if url.endswith('/robots.txt'):
                return ('User-agent: *\nAllow: /', 'text/plain', url)
            app.cancel_event.set()
            return ('<title>Saved</title><a href="/next">Next</a>', 'text/html', url)
        app.crawl_lock.acquire()
        with patch.object(app, 'public_url', side_effect=lambda url: url), patch.object(app, 'fetch', side_effect=fetch):
            app.crawl('https://example.com/', 10)
        run = app.crawl_history()[0]
        self.assertEqual(run['status'], 'cancelled')
        self.assertEqual(run['indexed'], 1)
        self.assertNotIn('https://example.com/next', fetched)
        self.assertEqual(len(app.search('Saved')), 1)
        self.assertFalse(app.crawl_lock.locked())
        self.assertFalse(app.state['running'])
        self.assertFalse(app.state['cancelling'])

    def test_cancel_before_first_request(self):
        app.cancel_event.set()
        app.crawl_lock.acquire()
        with patch.object(app, 'fetch') as fetch:
            app.crawl('https://example.com/', 10)
            fetch.assert_not_called()
        self.assertEqual(app.crawl_history()[0]['status'], 'cancelled')
        self.assertEqual(app.crawl_history()[0]['indexed'], 0)
        self.assertFalse(app.crawl_lock.locked())

    def test_site_filter_exact_host_and_counts(self):
        for host in ['example.com', 'docs.example.com', 'example.com.evil.test']:
            for i in range(12):
                app.save_page(f'https://{host}/{i:02}', '検索 Python', '検索本文')
        self.assertEqual(len(app.list_sites()), 3)
        self.assertTrue(all(site['count'] == 12 for site in app.list_sites()))
        for query in ['検索', 'Python']:
            result = app.search_page(query, page=2, site='EXAMPLE.COM.')
            self.assertEqual((result['total'], len(result['results'])), (12, 2))
            self.assertTrue(all(row['url'].startswith('https://example.com/') for row in result['results']))
            self.assertEqual(app.search_page(query, site='missing.test')['total'], 0)
            self.assertEqual(app.search_page(query, site="example.com' OR 1=1")['total'], 0)
        app.delete_page('https://example.com/00')
        self.assertEqual(next(site['count'] for site in app.list_sites() if site['host'] == 'example.com'), 11)

    def test_batch_refresh_preserves_failed_content_then_retries(self):
        urls = ['https://example.com/a', 'https://example.com/b']
        for url in urls:
            app.save_page(url, 'Original', 'old body')
        def fetch(url):
            if url.endswith('/robots.txt'):
                return '', 'text/plain', url
            if url.endswith('/b'):
                raise ValueError('一時的な取得失敗')
            return '<title>New</title><a href="/unexpected">ignore</a>', 'text/html', url
        app.crawl_lock.acquire()
        with patch.object(app, 'public_url', side_effect=lambda url: url), patch.object(app, 'fetch', side_effect=fetch), patch.object(app.cancel_event, 'wait', return_value=False):
            app.crawl(urls[0], 2, urls)
        self.assertEqual(app.crawl_history()[0]['status'], 'partial')
        self.assertEqual([row['url'] for row in app.failed_pages()], [urls[1]])
        self.assertEqual(len(app.search('Original')), 1)
        self.assertEqual(len(app.search('New')), 1)
        self.assertEqual(app.list_pages()['total'], 2)
        app.init_db()
        self.assertEqual(len(app.failed_pages()), 1)
        app.crawl_lock.acquire()
        with patch.object(app, 'public_url', side_effect=lambda url: url), patch.object(app, 'fetch', side_effect=lambda url: ('', 'text/plain', url) if url.endswith('/robots.txt') else ('<title>Recovered</title>', 'text/html', url)), patch.object(app.cancel_event, 'wait', return_value=False):
            app.crawl(urls[1], 1, [urls[1]])
        self.assertEqual(app.failed_pages(), [])
        self.assertEqual(len(app.search('Recovered')), 1)

    def test_success_clears_collector_failure(self):
        import collect
        collect.init_frontier()
        with app.connect() as db:
            db.execute("INSERT INTO frontier VALUES ('git','https://example.com/docs','failed','HTTP 503')")
        self.assertEqual(len(app.failed_pages('example.com')), 1)
        self.assertEqual(app.failed_pages('other.test'), [])
        app.record_failure('https://example.com/docs')
        self.assertEqual(app.failed_pages(), [])

    def test_title_words_beat_frequent_body_matches(self):
        app.save_page('https://example.com/tutorial', 'Tutorial', 'git log ' * 100)
        app.save_page('https://example.com/logging', 'Git logging guide', 'git log example')
        app.save_page('https://example.com/log', 'Git - git-log Documentation', 'git log ' + 'details ' * 1000)
        rows = app.search('git log')
        self.assertEqual(rows[0]['url'], 'https://example.com/log')
        self.assertEqual(app.title_match('Git logging', 'git log'), 0.5)
        self.assertEqual(app.title_match('logging', 'log'), 0)
        self.assertEqual(app.title_match('Log commands for Git', 'git log'), 1)
        self.assertEqual(app.title_match('Git - git-log', 'GIT LOG'), 2)

    def test_short_japanese_title_match_is_ranked_first(self):
        app.save_page('https://example.com/a', 'Guide', '京都 観光 ' * 100)
        app.save_page('https://example.com/z', '京都 観光', '京都 観光 information')
        self.assertEqual(app.search('京都 観光')[0]['url'], 'https://example.com/z')

    def test_partial_title_match_ranks_short_token_query(self):
        app.save_page('https://example.com/a', 'Unrelated manual', 'select order by ' * 100)
        app.save_page('https://example.com/z', 'SELECT', 'SELECT statements support ORDER BY')
        self.assertEqual(app.search('SELECT ORDER BY')[0]['url'], 'https://example.com/z')

    def test_fetch_enforces_limit_at_boundary(self):
        import io
        from email.message import Message
        headers = Message()
        headers['Content-Type'] = 'text/html; charset=utf-8'
        def response(body):
            stream = io.BytesIO(body)
            stream.headers = headers
            stream.url = 'https://example.com/'
            return stream
        with patch.object(app, 'MAX_BYTES', 32), patch.object(app, 'public_url', side_effect=lambda url: url), patch.object(app, 'build_opener') as opener:
            opener.return_value.open.return_value = response(b'x' * 32)
            self.assertEqual(len(app.fetch('https://example.com/')[0]), 32)
            opener.return_value.open.return_value = response(b'x' * 33)
            with self.assertRaisesRegex(ValueError, '大きすぎます'):
                app.fetch('https://example.com/')

    def test_short_index_updates_and_deletes(self):
        app.save_page('https://example.com/', '京都', '日本')
        self.assertEqual(len(app.search('京都')), 1)
        app.save_page('https://example.com/', '東京', '世界')
        self.assertEqual(app.search('京都'), [])
        self.assertEqual(app.search('日本'), [])
        self.assertEqual(len(app.search('東京')), 1)
        app.delete_page('https://example.com/')
        self.assertEqual(app.search('東京'), [])
        with app.connect() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM short_search').fetchone()[0], 0)

    def test_short_index_does_not_join_fields_or_interpret_punctuation(self):
        app.save_page('https://example.com/a', 'a', 'b É *日本')
        app.save_page('https://example.com/b', 'Other', 'é')
        self.assertEqual(app.search('ab'), [])
        self.assertEqual(len(app.search('*日')), 1)
        self.assertEqual(len(app.search('É')), 1)
        self.assertEqual(len(app.search('é')), 1)
        self.assertEqual(app.search('"*'), [])

    def test_existing_database_migrates_without_re_crawling(self):
        app.save_page('https://example.com/', '京都', '日本の資料')
        with app.connect() as db:
            db.execute('DROP TABLE short_search')
            db.execute('DROP TABLE search_meta')
        self.assertEqual(len(app.search('京都')), 1)  # Legacy read fallback.
        app.init_db()
        with app.connect() as db:
            self.assertTrue(app.short_index_ready(db))
            self.assertEqual(db.execute('SELECT count(*) FROM short_search').fetchone()[0], 1)
        self.assertEqual(len(app.search('京都')), 1)
        app.init_db()
        self.assertEqual(app.list_pages()['total'], 1)

    def test_mixed_query_uses_same_long_word_case_rules_as_fts(self):
        app.save_page('https://example.com/', 'Résumé', 'résumé x')
        self.assertEqual(len(app.search('RÉSUMÉ')), 1)
        self.assertEqual(len(app.search('RÉSUMÉ x')), 1)

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
