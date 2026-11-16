"""Collect a bounded documentation corpus, with persistent per-site frontiers."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import time
from urllib.error import HTTPError
from urllib.parse import urljoin, urlsplit, urlunsplit
from urllib.robotparser import RobotFileParser
import re

import app
from evaluate import CASES

SCOPES = {'python': '/ja/3/', 'sqlite': '/', 'mdn': '/ja/docs/Web',
          'postgresql': '/docs/current/', 'git': '/docs'}


def scoped_url(url, seed, scope):
    parts = urlsplit(url)
    path = parts.path or '/'
    if (parts.scheme not in ('http', 'https') or parts.netloc != urlsplit(seed).netloc
            or parts.username or parts.password or parts.query
            or not (path == scope.rstrip('/') or path.startswith(scope.rstrip('/') + '/'))
            or re.search(r'\.(pdf|zip|gz|png|jpg|svg|css|js|txt)$', path, re.I)):
        return None
    return urlunsplit((parts.scheme, parts.netloc, path, '', ''))


def init_frontier():
    with app.connect() as db:
        db.execute('CREATE TABLE IF NOT EXISTS frontier (site TEXT, url TEXT, status TEXT, reason TEXT, PRIMARY KEY(site,url))')
        # A killed process leaves its last request retryable.
        db.execute("UPDATE frontier SET status='pending' WHERE status='fetching'")


def site_count(seed):
    with app.connect() as db:
        return db.execute('SELECT count(*) FROM pages WHERE url_host(url)=?', (urlsplit(seed).hostname,)).fetchone()[0]


def collect_site(site, target=100, budget=500):
    seed, name = site['seed'], site['id']
    scope = SCOPES[name]
    robots = RobotFileParser()
    try:
        raw, _, _ = app.fetch(urljoin(seed, '/robots.txt'))
        robots.parse(raw.splitlines())
    except HTTPError as exc:
        if exc.code not in (404, 410):
            return {'site': name, 'error': f'robots.txt: HTTP {exc.code}', 'pages': site_count(seed)}
        robots.parse([])
    except Exception as exc:
        return {'site': name, 'error': f'robots.txt: {exc}', 'pages': site_count(seed)}
    delay = max(1, robots.crawl_delay(app.AGENT) or 0)
    with app.connect() as db:
        db.execute("INSERT OR IGNORE INTO frontier VALUES (?,?,'pending','')", (name, seed))
    attempts = 0
    last_request = time.monotonic()
    while site_count(seed) < target and attempts < budget:
        with app.connect() as db:
            row = db.execute("SELECT url FROM frontier WHERE site=? AND status='pending' ORDER BY rowid LIMIT 1", (name,)).fetchone()
            if not row:
                break
            url = row['url']
            db.execute("UPDATE frontier SET status='fetching' WHERE site=? AND url=?", (name, url))
        attempts += 1
        status, reason = 'done', ''
        try:
            if not robots.can_fetch(app.AGENT, url):
                status, reason = 'skipped', 'robots.txtで拒否'
            else:
                time.sleep(max(0, delay - (time.monotonic() - last_request)))
                last_request = time.monotonic()
                raw, mime, final = app.fetch(url)
                if not scoped_url(final, seed, scope):
                    status, reason = 'skipped', '収集範囲外へのリダイレクト'
                elif mime not in ('text/html', 'application/xhtml+xml'):
                    status, reason = 'skipped', f'HTML以外: {mime}'
                else:
                    page = app.Page()
                    page.feed(raw)
                    title = re.sub(r'\s+', ' ', ' '.join(page.title)).strip() or final
                    body = re.sub(r'\s+', ' ', ' '.join(page.text)).strip()
                    app.save_page(final, title, body)
                    app.record_failure(url)
                    links = {linked for link in page.links if (linked := scoped_url(urljoin(final, link), seed, scope))}
                    with app.connect() as db:
                        remaining = max(0, 3000 - db.execute('SELECT count(*) FROM frontier WHERE site=?', (name,)).fetchone()[0])
                        db.executemany("INSERT OR IGNORE INTO frontier VALUES (?,?,'pending','')", [(name, link) for link in sorted(links)[:remaining]])
        except Exception as exc:
            status, reason = 'failed', str(exc)
        if status == 'failed':
            app.record_failure(url, reason)
        with app.connect() as db:
            db.execute('UPDATE frontier SET status=?,reason=? WHERE site=? AND url=?', (status, reason, name, url))
        count = site_count(seed)
        if attempts % 10 == 0 or count >= target:
            print(f'{name}: {count}/{target} pages, {attempts} requests this run', flush=True)
    with app.connect() as db:
        outcomes = dict(db.execute('SELECT status,count(*) FROM frontier WHERE site=? GROUP BY status', (name,)).fetchall())
    return {'site': name, 'pages': site_count(seed), 'attempts': attempts, 'frontier': outcomes}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--target', type=int, default=100, help='Total indexed pages per host (1–500)')
    parser.add_argument('--budget', type=int, default=500, help='Maximum attempts per site per run (1–1000)')
    parser.add_argument('--retry-failed', action='store_true')
    args = parser.parse_args()
    if not 1 <= args.target <= 500 or not 1 <= args.budget <= 1000:
        parser.error('target must be 1–500 and budget must be 1–1000')
    app.init_db()
    init_frontier()
    if args.retry_failed:
        failures = app.failed_pages()
        origins = sorted({(urlsplit(row['url']).scheme, urlsplit(row['url']).netloc) for row in failures})
        for origin in origins:
            urls = [row['url'] for row in failures if (urlsplit(row['url']).scheme, urlsplit(row['url']).netloc) == origin][:args.budget]
            app.cancel_event.clear()
            app.crawl_lock.acquire()
            app.crawl(urls[0], len(urls), urls)
            print(f"{origin[0]}://{origin[1]}: {app.state['message']}", flush=True)
        if not origins:
            print('再取得する失敗ページはありません', flush=True)
        return
    sites = json.loads(CASES.read_text())['sites']
    with ThreadPoolExecutor(max_workers=5) as pool:
        results = list(pool.map(lambda site: collect_site(site, args.target, args.budget), sites))
    print(json.dumps(results, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
