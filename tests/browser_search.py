"""Optional browser tests: python -m unittest discover -s tests -p 'browser_*.py' -v."""
import shutil
import tempfile
import threading
import unittest
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from playwright.sync_api import sync_playwright
import app


class QuietHandler(app.Handler):
    def log_message(self, *args):
        pass


class SearchNavigationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.original_db = app.DB
        app.DB = Path(cls.temp.name) / 'browser.db'
        app.init_db()
        for i in range(23):
            host = 'example.com' if i < 13 else 'other.test'
            app.save_page(f'https://{host}/{i:02}', f'Python 検索 {i}', 'Python 日本語検索の本文')
        cls.server = app.ThreadingHTTPServer(('127.0.0.1', 0), QuietHandler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f'http://127.0.0.1:{cls.server.server_port}'
        cls.playwright = sync_playwright().start()
        cls.browser = cls.playwright.chromium.launch(executable_path=shutil.which('chromium'), args=['--no-sandbox'])

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.playwright.stop()
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)
        app.DB = cls.original_db
        cls.temp.cleanup()

    def setUp(self):
        self.context = self.browser.new_context(permissions=['clipboard-read', 'clipboard-write'])
        self.page = self.context.new_page()
        self.errors = []
        self.page.on('pageerror', lambda error: self.errors.append(str(error)))

    def tearDown(self):
        self.context.close()
        self.assertEqual(self.errors, [])

    def wait_results(self, total, start, end):
        text = f'{total}件の結果 · {start}–{end}件を表示'
        self.page.wait_for_function('(text)=>document.querySelector("#summary").textContent===text', arg=text)

    def params(self):
        return parse_qs(urlsplit(self.page.url).query)

    def test_direct_link_reload_and_clipboard(self):
        self.page.goto(self.base + '/?q=Python&site=example.com&page=2')
        self.wait_results(13, 11, 13)
        self.assertEqual(self.page.locator('#query').input_value(), 'Python')
        self.assertEqual(self.page.locator('#siteFilter').input_value(), 'example.com')
        self.assertEqual(self.page.locator('#results article').count(), 3)
        self.page.reload()
        self.wait_results(13, 11, 13)
        self.page.locator('#copySearch').click()
        self.page.wait_for_function("document.querySelector('#shareStatus').textContent.includes('コピーしました')")
        self.assertEqual(self.page.evaluate('navigator.clipboard.readText()'), self.page.url)

    def test_back_forward_across_query_page_site_and_empty_state(self):
        self.page.goto(self.base)
        self.page.locator('#query').fill('Python')
        self.page.locator('#searchForm button').click()
        self.wait_results(23, 1, 10)
        self.page.locator('#next').click()
        self.wait_results(23, 11, 20)
        self.page.locator('#siteFilter').select_option('other.test')
        self.wait_results(10, 1, 10)
        self.page.go_back()
        self.wait_results(23, 11, 20)
        self.assertEqual(self.params().get('page'), ['2'])
        self.assertEqual(self.page.locator('#siteFilter').input_value(), '')
        self.page.go_back()
        self.wait_results(23, 1, 10)
        self.page.go_back()
        self.page.wait_for_function("document.querySelector('#results').children.length===0")
        self.assertEqual(self.page.locator('#query').input_value(), '')
        self.assertTrue(self.page.locator('#copySearch').is_disabled())
        self.page.go_forward()
        self.wait_results(23, 1, 10)

    def test_clamped_page_and_invalid_page(self):
        self.page.goto(self.base + '/?q=Python&site=example.com&page=99999999999')
        self.wait_results(13, 11, 13)
        self.assertEqual(self.params()['page'], ['2'])
        self.page.goto(self.base + '/?q=Python&page=invalid')
        self.wait_results(23, 1, 10)
        self.assertNotIn('page', self.params())

    def test_missing_site_and_safe_query_text(self):
        self.page.goto(self.base + '/?q=%3Cscript%3Ealert(1)%3C%2Fscript%3E&site=missing.test&page=8')
        self.page.wait_for_function("document.querySelector('#summary').textContent.includes('見つかりませんでした')")
        self.assertEqual(self.page.locator('#siteFilter').input_value(), 'missing.test')
        self.assertEqual(self.page.locator('#query').input_value(), '<script>alert(1)</script>')
        self.assertNotIn('page', self.params())
        self.assertEqual(self.page.locator('#results script').count(), 0)

    def test_japanese_query_round_trip(self):
        self.page.goto(self.base)
        self.page.locator('#query').fill('日本語検索')
        self.page.locator('#searchForm button').click()
        self.wait_results(23, 1, 10)
        self.assertEqual(self.params()['q'], ['日本語検索'])
        self.page.reload()
        self.wait_results(23, 1, 10)
        self.assertEqual(self.page.locator('#query').input_value(), '日本語検索')

    def test_site_only_link_and_clipboard_failure(self):
        self.page.goto(self.base + '/?site=example.com&page=8')
        self.assertEqual(self.page.locator('#siteFilter').input_value(), 'example.com')
        self.assertNotIn('page', self.params())
        self.assertTrue(self.page.locator('#copySearch').is_disabled())
        self.page.locator('#query').fill('Python')
        self.page.locator('#searchForm button').click()
        self.wait_results(13, 1, 10)
        self.page.evaluate("() => { navigator.clipboard.writeText=()=>Promise.reject(new Error('Denied')); }")
        self.page.locator('#copySearch').click()
        self.page.wait_for_function("document.querySelector('#shareStatus').textContent.includes('アドレスバー')")
        self.assertNotIn('コピーしました', self.page.locator('#shareStatus').inner_text())

    def test_stale_response_does_not_restore_previous_url(self):
        delayed = []
        self.page.route('**/api/search?*', lambda route: delayed.append(route) if 'q=Python' in route.request.url else route.continue_())
        self.page.goto(self.base + '/?q=Python')
        self.page.wait_for_function("document.querySelector('#summary').textContent==='検索中…'")
        self.page.locator('#query').fill('missing')
        self.page.locator('#searchForm button').click()
        self.page.wait_for_function("document.querySelector('#summary').textContent.includes('見つかりませんでした')")
        self.assertTrue(delayed)
        for route in delayed:
            route.fulfill(json={'results': [], 'total': 0, 'page': 1, 'pages': 0})
        self.page.wait_for_timeout(100)
        self.assertEqual(self.params()['q'], ['missing'])
        self.assertEqual(self.page.locator('#query').input_value(), 'missing')


if __name__ == '__main__':
    unittest.main()
