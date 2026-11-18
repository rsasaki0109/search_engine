"""Small local web search engine. Run with python app.py."""
import argparse
import ipaddress
import json
import re
import socket
import sqlite3
import threading
import time
from contextlib import closing
from html.parser import HTMLParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import parse_qs, urljoin, urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
from urllib.robotparser import RobotFileParser

DB = Path(__file__).with_name('search.db')
AGENT = 'LittleSearch/1.0'
MAX_BYTES = 4_000_000
crawl_lock = threading.Lock()
cancel_event = threading.Event()
state = {'running': False, 'message': 'URLを登録して検索を始めましょう', 'indexed': 0, 'current_url': '', 'processed': 0, 'limit': 0, 'cancelling': False}
state_lock = threading.Lock()


def title_match(title, query):
    """Prefer whole query words in a title; punctuation separates words."""
    title_words = re.findall(r'\w+', title.casefold())
    query_words = re.findall(r'\w+', query.casefold())
    if not query_words:
        return 0
    size = len(query_words)
    if any(title_words[i:i + size] == query_words for i in range(len(title_words) - size + 1)):
        return 2
    return sum(word in title_words for word in set(query_words)) / len(set(query_words))


def normalized_title(title):
    return ' ' + ' '.join(re.findall(r'\w+', title.casefold())) + ' '


def title_order(query, column):
    words = re.findall(r'\w+', query.casefold())
    if not words:
        return '0 + 0', ()
    unique = sorted(set(words))
    matches = ' + '.join(f'(instr({column}, ?) > 0)' for _ in unique)
    order = f'CASE WHEN instr({column}, ?) > 0 THEN {len(unique) + 1} ELSE {matches} END'
    return order, (' ' + ' '.join(words) + ' ', *(' ' + word + ' ' for word in unique))


def short_terms(title, body):
    """Distinct exact character postings; a newline prevents cross-field bigrams."""
    text = (title + '\n' + body).translate(str.maketrans('ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'))
    terms = {f'u{ord(char):x}' for char in text}
    terms.update(f'b{ord(a):x}x{ord(b):x}' for a, b in zip(text, text[1:]))
    return ' '.join(sorted(terms))


def short_match_query(tokens):
    terms = []
    for token in tokens:
        text = token.translate(str.maketrans('ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'))
        if len(text) == 1:
            terms.append(f'u{ord(text):x}')
        else:
            terms.extend(f'b{ord(a):x}x{ord(b):x}' for a, b in zip(text, text[1:]))
    return ' AND '.join('"' + term + '"' for term in sorted(set(terms)))


def short_index_ready(db):
    if not db.execute("SELECT 1 FROM sqlite_master WHERE name='search_meta' AND type='table'").fetchone():
        return False
    row = db.execute("SELECT value FROM search_meta WHERE key='short_index_version'").fetchone()
    return bool(row and row[0] == '2')


def title_index_ready(db):
    return db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='search_titles'").fetchone() is not None


def connect():
    db = sqlite3.connect(DB, timeout=10)
    db.create_function('url_host', 1, lambda url: (urlsplit(url).hostname or '').lower().rstrip('.'), deterministic=True)
    db.create_function('title_match', 2, title_match, deterministic=True)
    db.row_factory = sqlite3.Row
    return db


def init_db():
    with closing(connect()) as db, db:
        db.execute('PRAGMA journal_mode=WAL')
        db.execute('BEGIN IMMEDIATE')
        db.execute('CREATE TABLE IF NOT EXISTS pages (url TEXT PRIMARY KEY, title TEXT, body TEXT, updated TEXT)')
        db.execute("CREATE VIRTUAL TABLE IF NOT EXISTS search USING fts5(url UNINDEXED, title, body, tokenize='trigram')")
        had_short_index = db.execute("SELECT 1 FROM sqlite_master WHERE name='short_search' AND type='table'").fetchone()
        db.execute("CREATE VIRTUAL TABLE IF NOT EXISTS short_search USING fts5(url UNINDEXED, terms, tokenize='ascii')")
        db.execute('CREATE TABLE IF NOT EXISTS search_meta (key TEXT PRIMARY KEY, value TEXT)')
        if not had_short_index or not short_index_ready(db):
            db.execute('DELETE FROM short_search')
            rows = db.execute('SELECT rowid,url,title,body FROM search')
            db.executemany('INSERT INTO short_search(rowid,url,terms) VALUES (?,?,?)', ((row['rowid'], row['url'], short_terms(row['title'], row['body'])) for row in rows))
            db.execute("INSERT OR REPLACE INTO search_meta VALUES ('short_index_version','2')")
        had_titles = title_index_ready(db)
        db.execute('CREATE TABLE IF NOT EXISTS search_titles (id INTEGER PRIMARY KEY, url TEXT NOT NULL, title TEXT NOT NULL, words TEXT NOT NULL, host TEXT NOT NULL)')
        if not had_titles or not db.execute("SELECT 1 FROM search_meta WHERE key='title_index_version'").fetchone():
            db.execute('DELETE FROM search_titles')
            rows = db.execute('SELECT rowid,url,title,url_host(url) AS host FROM search')
            db.executemany('INSERT INTO search_titles VALUES (?,?,?,?,?)',
                           ((row['rowid'], row['url'], row['title'], normalized_title(row['title']), row['host']) for row in rows))
            db.execute("INSERT OR REPLACE INTO search_meta VALUES ('title_index_version','1')")
        db.execute('CREATE INDEX IF NOT EXISTS search_titles_url ON search_titles(url)')
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
            raise ValueError('ページが大きすぎます（上限4MB）')
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


def remove_indexed_url(db, url, has_titles, has_short):
    ids_sql = 'SELECT id FROM search_titles WHERE url=?' if has_titles else 'SELECT rowid FROM search WHERE url=?'
    rowids = [row[0] for row in db.execute(ids_sql, (url,))]
    if has_short:
        db.executemany('DELETE FROM short_search WHERE rowid=?', ((rowid,) for rowid in rowids))
    db.executemany('DELETE FROM search WHERE rowid=?', ((rowid,) for rowid in rowids))
    if has_titles:
        db.execute('DELETE FROM search_titles WHERE url=?', (url,))


def save_page(url, title, body):
    terms = short_terms(title, body)
    with closing(connect()) as db, db:
        db.execute('INSERT OR REPLACE INTO pages VALUES (?, ?, ?, ?)', (url, title, body, utc_now()))
        has_titles = title_index_ready(db)
        has_short = short_index_ready(db)
        remove_indexed_url(db, url, has_titles, has_short)
        rowid = db.execute('INSERT INTO search VALUES (?, ?, ?)', (url, title, body)).lastrowid
        if has_titles:
            db.execute('INSERT INTO search_titles VALUES (?,?,?,?,?)',
                       (rowid, url, title, normalized_title(title), (urlsplit(url).hostname or '').lower().rstrip('.')))
        if has_short:
            db.execute('INSERT INTO short_search(rowid,url,terms) VALUES (?,?,?)', (rowid, url, terms))


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
    with closing(connect()) as db, db:
        deleted = db.execute('DELETE FROM pages WHERE url = ?', (url,)).rowcount
        remove_indexed_url(db, url, title_index_ready(db), short_index_ready(db))
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
    all_long = all(len(token) >= 3 for token in tokens)
    with closing(connect()) as db, db:
        # Counts, ranked IDs and deferred body reads must share one snapshot.
        # Let concurrent readers share mapped database pages via the OS page cache.
        db.execute('PRAGMA mmap_size=268435456')
        db.execute('BEGIN')
        has_titles = title_index_ready(db)
        if all_long:
            match = ' AND '.join('"' + t.replace('"', '""') + '"' for t in tokens)
            source, where, params = 'search', 'search MATCH ?', (match,)
            order = 'bm25(search,0,5,1), url'
        else:
            # Intersect posting lists once; do not rerun an FTS MATCH for each candidate rowid.
            long_tokens = [token for token in tokens if len(token) >= 3]
            short_tokens = [token for token in tokens if len(token) < 3]
            indexed = short_index_ready(db)
            source = 'search' if indexed or long_tokens else 'pages'
            clauses, params = [], ()
            if indexed:
                candidates = []
                if long_tokens:
                    candidates.append('SELECT rowid FROM search WHERE search MATCH ?')
                    params += (' AND '.join('"' + token.replace('"', '""') + '"' for token in long_tokens),)
                candidates.append('SELECT rowid FROM short_search WHERE short_search MATCH ?')
                params += (short_match_query(short_tokens),)
                candidate_id = 't.id' if has_titles else 'rowid'
                clauses.append(f'{candidate_id} IN (' + ' INTERSECT '.join(candidates) + ')')
            elif long_tokens:
                clauses.append('search MATCH ?')
                params += (' AND '.join('"' + token.replace('"', '""') + '"' for token in long_tokens),)
            if not indexed:
                clauses.extend('(instr(lower(title),lower(?))>0 OR instr(lower(body),lower(?))>0)' for _ in tokens)
                params += tuple(token for token in tokens for _ in range(2))
            where, order = ' AND '.join(clauses), 'url'
        body_source = source
        url_column, title_column = 'url', 'title'
        id_column, host_expr = 'rowid', 'url_host(url)'
        rank, rank_params = 'title_match(title, ?)', (query,)
        if has_titles:
            if all_long:
                source = 'search JOIN search_titles AS t ON t.id = search.rowid'
                order = 'bm25(search,0,5,1), t.url'
            elif indexed:
                source = 'search_titles AS t'
                order = 't.url'
            if source != body_source:
                url_column, title_column = 't.url', 't.title'
                id_column, host_expr = 't.id', 't.host'
                rank, rank_params = title_order(query, 't.words')
        if site.strip():
            where = f'({where}) AND {host_expr} = ?'
            params = (*params, site.strip().lower().rstrip('.'))
        count_source = body_source if all_long and not site.strip() else source
        total = db.execute(f'SELECT count(*) FROM {count_source} WHERE {where}', params).fetchone()[0]
        pages = (total + page_size - 1) // page_size
        page = min(page, max(1, pages))
        rows = db.execute(
            f'SELECT {id_column} AS rowid,{url_column} AS url,{title_column} AS title FROM {source} '
            f'WHERE {where} ORDER BY {rank} DESC, {order} LIMIT ? OFFSET ?',
            (*params, *rank_params, page_size, (page - 1) * page_size),
        ).fetchall()
        bodies = {}
        if rows:
            placeholders = ','.join('?' for _ in rows)
            bodies = {row['rowid']: row['body'] for row in db.execute(f'SELECT rowid,body FROM {body_source} WHERE rowid IN ({placeholders})', tuple(row['rowid'] for row in rows))}
        results = [{'url': row['url'], 'title': row['title'], 'excerpt': excerpt(bodies[row['rowid']], tokens)} for row in rows]
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
