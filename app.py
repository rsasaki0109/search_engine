"""Small local web search engine. Run with python app.py."""
import argparse
import ipaddress
import json
import re
import socket
import sqlite3
import threading
import time
from html.parser import HTMLParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import parse_qs, urljoin, urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
from urllib.robotparser import RobotFileParser

DB = Path(__file__).with_name('search.db')
AGENT = 'LittleSearch/1.0'
MAX_BYTES = 2_000_000
crawl_lock = threading.Lock()
state = {'running': False, 'message': 'URLを登録して検索を始めましょう', 'indexed': 0}
state_lock = threading.Lock()


def connect():
    db = sqlite3.connect(DB, timeout=10)
    db.row_factory = sqlite3.Row
    return db


def init_db():
    with connect() as db:
        db.execute('CREATE TABLE IF NOT EXISTS pages (url TEXT PRIMARY KEY, title TEXT, body TEXT, updated TEXT)')
        db.execute("CREATE VIRTUAL TABLE IF NOT EXISTS search USING fts5(url UNINDEXED, title, body, tokenize='trigram')")


def public_url(url):
    parts = urlsplit(url)
    if parts.scheme not in ('http', 'https') or not parts.hostname or parts.username or parts.password:
        raise ValueError('公開された http / https のURLを入力してください')
    if parts.port not in (None, 80, 443):
        raise ValueError('標準のHTTP/HTTPSポートのみ使用できます')
    addresses = socket.getaddrinfo(parts.hostname, parts.port or (443 if parts.scheme == 'https' else 80), type=socket.SOCK_STREAM)
    if not addresses or any(not ipaddress.ip_address(a[4][0]).is_global for a in addresses):
        raise ValueError('ローカル・プライベートネットワークにはアクセスできません')
    return urlunsplit((parts.scheme, parts.netloc, parts.path or '/', parts.query, ''))


class Redirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return super().redirect_request(req, fp, code, msg, headers, public_url(newurl))


def fetch(url):
    url = public_url(url)
    with build_opener(Redirects()).open(Request(url, headers={'User-Agent': AGENT}), timeout=12) as response:
        content = response.read(MAX_BYTES + 1)
        if len(content) > MAX_BYTES:
            raise ValueError('ページが大きすぎます（上限2MB）')
        return content.decode(response.headers.get_content_charset() or 'utf-8', errors='replace'), response.headers.get_content_type(), response.url


class Page(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.hidden = 0
        self.in_title = False
        self.title, self.text, self.links = [], [], []

    def handle_starttag(self, tag, attrs):
        if tag in ('script', 'style', 'noscript'):
            self.hidden += 1
        if tag == 'title':
            self.in_title = True
        if tag == 'a':
            self.links.extend(v for k, v in attrs if k == 'href' and v)

    def handle_endtag(self, tag):
        if tag in ('script', 'style', 'noscript'):
            self.hidden = max(0, self.hidden - 1)
        if tag == 'title':
            self.in_title = False

    def handle_data(self, data):
        if not self.hidden:
            self.text.append(data)
            if self.in_title:
                self.title.append(data)


def save_page(url, title, body):
    with connect() as db:
        db.execute('INSERT OR REPLACE INTO pages VALUES (?, ?, ?, ?)', (url, title, body, time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime())))
        db.execute('DELETE FROM search WHERE url = ?', (url,))
        db.execute('INSERT INTO search VALUES (?, ?, ?)', (url, title, body))


def search(query):
    query = query.strip()[:200]
    if not query:
        return []
    tokens = query.split()
    with connect() as db:
        if all(len(t) >= 3 for t in tokens):
            match = ' AND '.join('"' + t.replace('"', '""') + '"' for t in tokens)
            rows = db.execute('SELECT url,title,substr(body,1,400) AS excerpt FROM search WHERE search MATCH ? ORDER BY bm25(search,0,5,1) LIMIT 50', (match,)).fetchall()
        else:
            # FTS trigram cannot match one- or two-character Japanese queries.
            clauses = ' AND '.join('(instr(lower(title),lower(?))>0 OR instr(lower(body),lower(?))>0)' for _ in tokens)
            rows = db.execute(f'SELECT url,title,substr(body,1,400) AS excerpt FROM pages WHERE {clauses} LIMIT 50', tuple(t for t in tokens for _ in range(2))).fetchall()
        return [dict(row) for row in rows]


def crawl(seed, limit):
    indexed, errors = 0, []
    try:
        seed = public_url(seed)
        origin = urlsplit(seed).netloc
        robots = RobotFileParser()
        try:
            raw, _, _ = fetch(urljoin(seed, '/robots.txt'))
            robots.parse(raw.splitlines())
        except HTTPError as exc:
            if exc.code not in (404, 410):
                raise ValueError('robots.txtを確認できませんでした') from exc
            robots.parse([])
        queue, seen = [seed], set()
        while queue and len(seen) < limit:
            url = queue.pop(0)
            if url in seen:
                continue
            seen.add(url)
            if not robots.can_fetch(AGENT, url):
                errors.append('robots.txtで拒否: ' + url)
                continue
            try:
                raw, mime, final = fetch(url)
                if urlsplit(final).netloc != origin:
                    raise ValueError('別サイトへのリダイレクトは対象外です')
                if mime not in ('text/html', 'application/xhtml+xml'):
                    continue
                page = Page()
                page.feed(raw)
                title = re.sub(r'\s+', ' ', ' '.join(page.title)).strip() or final
                body = re.sub(r'\s+', ' ', ' '.join(page.text)).strip()
                save_page(final, title, body)
                indexed += 1
                for link in page.links:
                    parts = urlsplit(urljoin(final, link))
                    linked = urlunsplit((parts.scheme, parts.netloc, parts.path or '/', parts.query, ''))
                    if parts.scheme in ('http', 'https') and parts.netloc == origin and linked not in seen and linked not in queue and len(queue) < 200:
                        queue.append(linked)
                with state_lock:
                    state.update(message=f'{indexed}ページを登録中…', indexed=indexed)
            except Exception as exc:
                errors.append(f'{url}: {exc}')
            time.sleep(1)
        message = f'{indexed}ページを登録しました'
        if errors:
            message += f'（スキップ・エラー {len(errors)}件: {errors[0]}）'
    except Exception as exc:
        message = str(exc)
    finally:
        with state_lock:
            state.update(running=False, message=message, indexed=indexed)
        crawl_lock.release()


class Handler(BaseHTTPRequestHandler):
    def reply(self, value, status=200):
        data = json.dumps(value, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        path = urlsplit(self.path)
        if path.path == '/api/search':
            self.reply({'results': search(parse_qs(path.query).get('q', [''])[0])})
        elif path.path == '/api/status':
            with connect() as db:
                count = db.execute('SELECT count(*) FROM pages').fetchone()[0]
            with state_lock:
                snapshot = dict(state)
            self.reply({**snapshot, 'total': count})
        elif path.path == '/':
            data = Path(__file__).with_name('index.html').read_bytes()
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        else:
            self.reply({'error': 'Not found'}, 404)

    def do_POST(self):
        if self.path != '/api/crawl':
            return self.reply({'error': 'Not found'}, 404)
        # Disallow requests from other websites to this local service.
        if self.headers.get('Origin') and self.headers['Origin'] != 'http://' + self.headers.get('Host', ''):
            return self.reply({'error': 'Origin is not allowed'}, 403)
        if self.headers.get('Content-Type', '').split(';')[0] != 'application/json':
            return self.reply({'error': 'JSON required'}, 415)
        try:
            length = int(self.headers.get('Content-Length', '0'))
            if not 0 < length <= 4096:
                raise ValueError('入力が大きすぎるか空です')
            data = json.loads(self.rfile.read(length))
            seed = str(data.get('url', '')).strip()
            if len(seed) > 2048 or urlsplit(seed).scheme not in ('http', 'https'):
                raise ValueError('http / https のURLを入力してください')
            limit = max(1, min(30, int(data.get('limit', 10))))
        except (ValueError, TypeError, AttributeError) as exc:
            return self.reply({'error': str(exc)}, 400)
        if not crawl_lock.acquire(blocking=False):
            return self.reply({'error': 'クロール実行中です'}, 409)
        with state_lock:
            state.update(running=True, message='robots.txtを確認中…', indexed=0)
        threading.Thread(target=crawl, args=(seed, limit), daemon=True).start()
        self.reply({'ok': True}, 202)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--port', type=int, default=8000)
    args = parser.parse_args()
    init_db()
    print(f'Little Search: http://127.0.0.1:{args.port}', flush=True)
    ThreadingHTTPServer(('127.0.0.1', args.port), Handler).serve_forever()
