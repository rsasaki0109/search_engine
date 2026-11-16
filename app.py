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
cancel_event = threading.Event()
state = {'running': False, 'message': 'URLを登録して検索を始めましょう', 'indexed': 0, 'current_url': '', 'processed': 0, 'limit': 0, 'cancelling': False}
state_lock = threading.Lock()


def connect():
    db = sqlite3.connect(DB, timeout=10)
    db.create_function('url_host', 1, lambda url: (urlsplit(url).hostname or '').lower().rstrip('.'), deterministic=True)
    db.row_factory = sqlite3.Row
    return db


def init_db():
    with connect() as db:
        db.execute('CREATE TABLE IF NOT EXISTS pages (url TEXT PRIMARY KEY, title TEXT, body TEXT, updated TEXT)')
        db.execute("CREATE VIRTUAL TABLE IF NOT EXISTS search USING fts5(url UNINDEXED, title, body, tokenize='trigram')")
        db.execute('CREATE TABLE IF NOT EXISTS failures (url TEXT PRIMARY KEY, reason TEXT, updated TEXT)')
        db.execute("CREATE TABLE IF NOT EXISTS crawls (id INTEGER PRIMARY KEY, seed TEXT, started TEXT, finished TEXT, status TEXT, indexed INTEGER, message TEXT, events TEXT)")
        db.execute("UPDATE crawls SET status='interrupted', finished=?, message='サーバー停止により中断しました' WHERE status='running'", (utc_now(),))


def utc_now():
    return time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime())


def crawl_history():
    with connect() as db:
        rows = db.execute('SELECT * FROM crawls ORDER BY id DESC LIMIT 20').fetchall()
    return [{**dict(row), 'events': json.loads(row['events'])} for row in rows]


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


def list_pages(query='', page=1):
    page = max(1, min(1000000, page))
    params = (query.strip()[:200],) * 2
    where = 'instr(lower(title),lower(?))>0 OR instr(lower(url),lower(?))>0'
    with connect() as db:
        total = db.execute(f'SELECT count(*) FROM pages WHERE {where}', params).fetchone()[0]
        pages = (total + 9) // 10
        page = min(page, max(1, pages))
        rows = db.execute(f'SELECT url,title,updated FROM pages WHERE {where} ORDER BY updated DESC,url LIMIT 10 OFFSET ?', (*params, (page - 1) * 10)).fetchall()
    return {'results': [dict(row) for row in rows], 'total': total, 'page': page, 'pages': pages}


def delete_page(url):
    with connect() as db:
        deleted = db.execute('DELETE FROM pages WHERE url = ?', (url,)).rowcount
        db.execute('DELETE FROM search WHERE url = ?', (url,))
    return bool(deleted)


def excerpt(body, tokens, size=260):
    pattern = re.compile('|'.join(re.escape(t) for t in tokens), re.IGNORECASE)
    found = pattern.search(body)
    start = max(0, found.start() - 70) if found else 0
    end = min(len(body), start + size)
    return ('…' if start else '') + body[start:end] + ('…' if end < len(body) else '')


def list_sites():
    with connect() as db:
        return [dict(row) for row in db.execute('SELECT url_host(url) AS host, count(*) AS count FROM pages GROUP BY url_host(url) ORDER BY host')]


def search_page(query, page=1, page_size=10, site=''):
    query = query.strip()[:200]
    page = max(1, min(1000000, page))
    if not query:
        return {'results': [], 'total': 0, 'page': 1, 'pages': 0}
    tokens = query.split()
    with connect() as db:
        if all(len(t) >= 3 for t in tokens):
            match = ' AND '.join('"' + t.replace('"', '""') + '"' for t in tokens)
            source, where, params = 'search', 'search MATCH ?', (match,)
            order = 'bm25(search,0,5,1), url'
        else:
            # FTS trigram cannot match one- or two-character Japanese queries.
            source = 'pages'
            where = ' AND '.join('(instr(lower(title),lower(?))>0 OR instr(lower(body),lower(?))>0)' for _ in tokens)
            params = tuple(t for t in tokens for _ in range(2))
            order = 'url'
        if site.strip():
            where = f'({where}) AND url_host(url) = ?'
            params = (*params, site.strip().lower().rstrip('.'))
        total = db.execute(f'SELECT count(*) FROM {source} WHERE {where}', params).fetchone()[0]
        pages = (total + page_size - 1) // page_size
        page = min(page, max(1, pages))
        rows = db.execute(f'SELECT url,title,body FROM {source} WHERE {where} ORDER BY {order} LIMIT ? OFFSET ?', (*params, page_size, (page - 1) * page_size)).fetchall()
        results = [{'url': row['url'], 'title': row['title'], 'excerpt': excerpt(row['body'], tokens)} for row in rows]
        return {'results': results, 'total': total, 'page': page, 'pages': pages}


def search(query):
    return search_page(query, page_size=50)['results']


def failed_pages(host=''):
    with connect() as db:
        failures = {row['url']: dict(row) for row in db.execute('SELECT url,reason FROM failures')}
        if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='frontier'").fetchone():
            for row in db.execute("SELECT url,reason FROM frontier WHERE status='failed'"):
                failures.setdefault(row['url'], dict(row))
    return [row for url, row in sorted(failures.items()) if not host or urlsplit(url).hostname == host]


def record_failure(url, reason=None):
    with connect() as db:
        if reason is None:
            db.execute('DELETE FROM failures WHERE url=?', (url,))
            if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='frontier'").fetchone():
                db.execute("UPDATE frontier SET status='done',reason='' WHERE url=? AND status='failed'", (url,))
        else:
            db.execute('INSERT OR REPLACE INTO failures VALUES (?,?,?)', (url, reason, utc_now()))


def crawl(seed, limit, urls=None):
    indexed, errors, events = 0, [], []
    run_id = None
    message, outcome = 'クロールを開始できませんでした', 'failed'
    def record(url, status, reason):
        events.append({'url': url, 'status': status, 'reason': reason})
        if status == 'failed':
            record_failure(url, reason)
        elif status == 'success':
            record_failure(url)
        with connect() as db:
            db.execute('UPDATE crawls SET indexed=?, events=? WHERE id=?', (indexed, json.dumps(events, ensure_ascii=False), run_id))
        with state_lock:
            state.update(processed=len(events), indexed=indexed)
    try:
        with connect() as db:
            run_id = db.execute("INSERT INTO crawls(seed,started,status,indexed,message,events) VALUES (?,?,'running',0,'クロール中','[]')", (seed, utc_now())).lastrowid
            db.execute('DELETE FROM crawls WHERE id NOT IN (SELECT id FROM crawls ORDER BY id DESC LIMIT 20)')
        with state_lock:
            state.update(current_url=seed, processed=0, limit=limit)
        if cancel_event.is_set():
            raise ValueError('停止要求を受け付けました')
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
        queue, seen = list(dict.fromkeys(urls)) if urls is not None else [seed], set()
        delay = max(1, robots.crawl_delay(AGENT) or 0)
        if cancel_event.wait(delay):
            raise ValueError('停止要求を受け付けました')
        while queue and len(seen) < limit and not cancel_event.is_set():
            url = queue.pop(0)
            if url in seen:
                continue
            seen.add(url)
            if urlsplit(url).netloc != origin:
                record(url, 'failed', '別サイトのURLは一括処理できません')
                continue
            with state_lock:
                state.update(current_url=url)
            if not robots.can_fetch(AGENT, url):
                errors.append('robots.txtで拒否: ' + url)
                record(url, 'skipped', 'robots.txtで拒否')
                continue
            try:
                raw, mime, final = fetch(url)
                if urlsplit(final).netloc != origin:
                    raise ValueError('別サイトへのリダイレクトは対象外です')
                if mime not in ('text/html', 'application/xhtml+xml'):
                    record(url, 'skipped', 'HTML以外のコンテンツ: ' + mime)
                    cancel_event.wait(delay)
                    continue
                page = Page()
                page.feed(raw)
                title = re.sub(r'\s+', ' ', ' '.join(page.title)).strip() or final
                body = re.sub(r'\s+', ' ', ' '.join(page.text)).strip()
                save_page(final, title, body)
                indexed += 1
                record(url, 'success', '登録しました')
                for link in (page.links if urls is None else []):
                    parts = urlsplit(urljoin(final, link))
                    linked = urlunsplit((parts.scheme, parts.netloc, parts.path or '/', parts.query, ''))
                    if parts.scheme in ('http', 'https') and parts.netloc == origin and linked not in seen and linked not in queue and len(queue) < 200:
                        queue.append(linked)
                with state_lock:
                    state.update(message=f'{indexed}ページを登録中…', indexed=indexed)
            except Exception as exc:
                errors.append(f'{url}: {exc}')
                record(url, 'failed', str(exc))
            cancel_event.wait(delay)
        outcome = 'completed' if not errors else 'partial'
        message = f'{indexed}ページを登録しました'
        if errors:
            message += f'（スキップ・エラー {len(errors)}件: {errors[0]}）'
    except Exception as exc:
        message = str(exc)
        if not cancel_event.is_set():
            for failed in (urls if urls is not None else [seed]):
                record_failure(failed, message)
    finally:
        try:
            with state_lock:
                if cancel_event.is_set():
                    outcome = 'cancelled'
                    message = f'クロールを停止しました（{indexed}ページ保存済み）'
                try:
                    if run_id is not None:
                        with connect() as db:
                            db.execute('UPDATE crawls SET finished=?,status=?,indexed=?,message=? WHERE id=?', (utc_now(), outcome, indexed, message, run_id))
                finally:
                    state.update(running=False, cancelling=False, message=message, indexed=indexed, current_url='')
        finally:
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
            params = parse_qs(path.query)
            try:
                page = int(params.get('page', ['1'])[0])
            except ValueError:
                return self.reply({'error': 'ページ番号は整数を指定してください'}, 400)
            self.reply(search_page(params.get('q', [''])[0], page, site=params.get('site', [''])[0]))
        elif path.path == '/api/pages':
            params = parse_qs(path.query)
            try:
                page = int(params.get('page', ['1'])[0])
            except ValueError:
                return self.reply({'error': 'ページ番号は整数を指定してください'}, 400)
            self.reply(list_pages(params.get('q', [''])[0], page))
        elif path.path == '/api/sites':
            self.reply({'results': list_sites()})
        elif path.path == '/api/failures':
            self.reply({'results': failed_pages(parse_qs(path.query).get('host', [''])[0])})
        elif path.path == '/api/crawls':
            self.reply({'results': crawl_history()})
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
        if self.path not in ('/api/crawl', '/api/pages/delete', '/api/crawl/cancel', '/api/sites/refresh', '/api/sites/retry'):
            return self.reply({'error': 'Not found'}, 404)
        # Disallow requests from other websites to this local service.
        if self.headers.get('Origin') and self.headers['Origin'] != 'http://' + self.headers.get('Host', ''):
            return self.reply({'error': 'Origin is not allowed'}, 403)
        if self.headers.get('Content-Type', '').split(';')[0] != 'application/json':
            return self.reply({'error': 'JSON required'}, 415)
        if self.path == '/api/crawl/cancel':
            with state_lock:
                if not state['running']:
                    return self.reply({'error': '実行中のクロールはありません'}, 409)
                cancel_event.set()
                state.update(cancelling=True, message='停止要求を受け付けました。処理中の取得が終了するまでお待ちください')
            return self.reply({'ok': True}, 202)
        urls = None
        try:
            length = int(self.headers.get('Content-Length', '0'))
            if not 0 < length <= 4096:
                raise ValueError('入力が大きすぎるか空です')
            data = json.loads(self.rfile.read(length))
            if self.path in ('/api/sites/refresh', '/api/sites/retry'):
                host = str(data.get('host', '')).strip().lower().rstrip('.')
                if not host:
                    raise ValueError('サイトを選択してください')
                if self.path == '/api/sites/refresh':
                    with connect() as db:
                        urls = [row['url'] for row in db.execute('SELECT url FROM pages WHERE url_host(url)=? ORDER BY url', (host,))]
                else:
                    urls = [row['url'] for row in failed_pages(host)]
                if not urls:
                    raise ValueError('対象ページがありません')
                if len(urls) > 500:
                    raise ValueError('一括処理は500ページまでです')
                # Keep a single origin for robots.txt and politeness rules.
                origin = (urlsplit(urls[0]).scheme, urlsplit(urls[0]).netloc)
                if any((urlsplit(url).scheme, urlsplit(url).netloc) != origin for url in urls):
                    raise ValueError('同じ接続先のページを選択してください')
                seed, limit = urls[0], len(urls)
            else:
                seed = str(data.get('url', '')).strip()
                if len(seed) > 2048 or urlsplit(seed).scheme not in ('http', 'https'):
                    raise ValueError('http / https のURLを入力してください')
                limit = max(1, min(30, int(data.get('limit', 10))))
        except (ValueError, TypeError, AttributeError) as exc:
            return self.reply({'error': str(exc)}, 400)
        if self.path == '/api/pages/delete':
            if not crawl_lock.acquire(blocking=False):
                return self.reply({'error': 'クロール完了後に削除してください'}, 409)
            try:
                deleted = delete_page(seed)
            finally:
                crawl_lock.release()
            return self.reply({'ok': True} if deleted else {'error': 'ページが見つかりません'}, 200 if deleted else 404)
        if not crawl_lock.acquire(blocking=False):
            return self.reply({'error': 'クロール実行中です'}, 409)
        with state_lock:
            cancel_event.clear()
            state.update(cancelling=False, running=True, message='robots.txtを確認中…', indexed=0, processed=0, current_url=seed, limit=limit)
        threading.Thread(target=crawl, args=(seed, limit) if urls is None else (seed, limit, urls), daemon=True).start()
        self.reply({'ok': True}, 202)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--port', type=int, default=8000)
    args = parser.parse_args()
    init_db()
    print(f'Little Search: http://127.0.0.1:{args.port}', flush=True)
    ThreadingHTTPServer(('127.0.0.1', args.port), Handler).serve_forever()
