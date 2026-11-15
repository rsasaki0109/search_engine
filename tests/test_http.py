"""HTTP integration tests using temporary data and no external network access."""
import http.client
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

import app


class QuietHandler(app.Handler):
    def log_message(self, *args):
        pass


class HTTPTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.original_db = app.DB
        self.original_state = dict(app.state)
        self.addCleanup(self.restore_data)
        app.DB = Path(self.temp.name) / 'http.db'
        app.init_db()
        app.cancel_event.clear()
        app.state.update(running=False, cancelling=False)
        self.server = app.ThreadingHTTPServer(('127.0.0.1', 0), QuietHandler)
        self.worker = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.worker.start()
        self.addCleanup(self.cleanup)

    def cleanup(self):
        self.server.shutdown()
        self.server.server_close()
        self.worker.join(timeout=5)

    def restore_data(self):
        app.DB = self.original_db
        app.state.clear()
        app.state.update(self.original_state)
        app.cancel_event.clear()
        self.temp.cleanup()

    def request(self, method, path, body=None, headers=None):
        connection = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=5)
        try:
            connection.request(method, path, body, headers or {})
            response = connection.getresponse()
            content = response.read().decode('utf-8')
            return response.status, content
        finally:
            connection.close()

    def post(self, path, data, headers=None):
        return self.request('POST', path, json.dumps(data), {'Content-Type': 'application/json', **(headers or {})})

    def test_home_search_and_pagination(self):
        status, html = self.request('GET', '/')
        self.assertEqual(status, 200)
        self.assertIn('Little Search', html)
        for i in range(12):
            app.save_page(f'https://example.com/{i:02}', 'Python', 'Searchable body')
        status, body = self.request('GET', '/api/search?q=Python&page=2')
        data = json.loads(body)
        self.assertEqual(status, 200)
        self.assertEqual((data['total'], data['page'], len(data['results'])), (12, 2, 2))
        self.assertEqual(self.request('GET', '/api/search?page=invalid')[0], 400)
        self.assertEqual(self.request('GET', '/api/pages?page=invalid')[0], 400)
        self.assertEqual(self.request('GET', '/missing')[0], 404)

    def test_delete_updates_search_library_and_count(self):
        url = 'https://example.com/'
        app.save_page(url, 'Python', 'Searchable body')
        self.assertEqual(json.loads(self.request('GET', '/api/pages')[1])['total'], 1)
        self.assertEqual(self.post('/api/pages/delete', {'url': url})[0], 200)
        self.assertEqual(json.loads(self.request('GET', '/api/status')[1])['total'], 0)
        self.assertEqual(json.loads(self.request('GET', '/api/search?q=Python')[1])['results'], [])
        self.assertEqual(self.post('/api/pages/delete', {'url': url})[0], 404)

    def test_mutations_reject_cross_origin_and_non_json(self):
        for path in ['/api/crawl', '/api/crawl/cancel', '/api/pages/delete']:
            with self.subTest(path=path):
                self.assertEqual(self.post(path, {}, {'Origin': 'https://other.example'})[0], 403)
                self.assertEqual(self.request('POST', path, '{}')[0], 415)

    def test_invalid_request_does_not_start_crawl(self):
        for body in ['{', '[]', '{}', '{"url":"file:///etc/passwd"}', '{"url":"https://example.com", "limit":"bad"}']:
            with self.subTest(body=body):
                status, _ = self.request('POST', '/api/crawl', body, {'Content-Type': 'application/json'})
                self.assertEqual(status, 400)
        self.assertFalse(app.state['running'])
        self.assertEqual(app.crawl_history(), [])

    def test_cancel_and_conflicts(self):
        self.assertEqual(self.post('/api/crawl/cancel', {})[0], 409)
        with app.state_lock:
            app.state.update(running=True)
        self.assertEqual(self.post('/api/crawl/cancel', {})[0], 202)
        self.assertTrue(app.cancel_event.is_set())
        self.assertTrue(app.state['cancelling'])
        app.crawl_lock.acquire()
        try:
            for path in ['/api/crawl', '/api/pages/delete']:
                self.assertEqual(self.post(path, {'url': 'https://example.com/'})[0], 409)
        finally:
            app.crawl_lock.release()

    def test_crawl_through_http_persists_history(self):
        done = threading.Event()
        original_crawl = app.crawl
        def crawl(seed, limit):
            try:
                original_crawl(seed, limit)
            finally:
                done.set()
        def fetch(url):
            if url.endswith('/robots.txt'):
                return 'User-agent: *\nAllow: /', 'text/plain', url
            return '<title>Python demo</title><p>Searchable body</p>', 'text/html', url
        with patch.object(app, 'crawl', side_effect=crawl), patch.object(app, 'public_url', side_effect=lambda url: url), patch.object(app, 'fetch', side_effect=fetch), patch.object(app.cancel_event, 'wait', return_value=False):
            self.assertEqual(self.post('/api/crawl', {'url': 'https://example.com/', 'limit': 1})[0], 202)
            self.assertTrue(done.wait(5), 'Crawler did not finish')
        status, body = self.request('GET', '/api/crawls')
        self.assertEqual(status, 200)
        run = json.loads(body)['results'][0]
        self.assertEqual((run['status'], run['indexed']), ('completed', 1))
        self.assertEqual(run['events'][0]['status'], 'success')
        self.assertFalse(app.state['running'])


if __name__ == '__main__':
    unittest.main()
