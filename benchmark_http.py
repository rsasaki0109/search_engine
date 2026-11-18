"""Benchmark the loopback search API against a disposable, scaled copy of an index."""
import argparse
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import hashlib
import http.client
import json
from pathlib import Path
import platform
import random
import sqlite3
import tempfile
import threading
import time
from urllib.parse import urlencode

import app
from benchmark import CASES, EXTRA_QUERIES, percentile


class QuietHandler(app.Handler):
    def log_message(self, *args):
        pass


def copy_index(source, destination):
    with closing(sqlite3.connect(source.as_uri() + '?mode=ro', uri=True)) as original:
        with closing(sqlite3.connect(destination)) as copy:
            original.backup(copy)


def build_corpus(source, destination, target):
    copy_index(source, destination)
    app.DB = destination
    app.init_db()
    with closing(app.connect()) as db, db:
        rows = db.execute('SELECT url,title,body FROM pages ORDER BY url').fetchall()
        if not rows or len(rows) > target:
            raise ValueError('Source must have between 1 and the target number of pages')
        # Use excerpts of varied length, with distinct URLs. The original documents remain intact.
        rng = random.Random(20261118)
        for i in range(target - len(rows)):
            row = rows[i % len(rows)]
            url = f'https://bench-{i % 20}.invalid/doc/{i}'
            title = row['title']
            body = row['body']
            start = rng.randrange(max(1, len(body) - 500))
            excerpt = body[start:start + rng.randint(500, 4500)]
            db.execute('INSERT INTO pages VALUES (?,?,?,?)', (url, title, excerpt, 'synthetic'))
            rowid = db.execute('INSERT INTO search VALUES (?,?,?)', (url, title, excerpt)).lastrowid
            db.execute('INSERT INTO search_titles VALUES (?,?,?,?,?)',
                       (rowid, url, title, app.normalized_title(title), f'bench-{i % 20}.invalid'))
            db.execute('INSERT INTO short_search(rowid,url,terms) VALUES (?,?,?)',
                       (rowid, url, app.short_terms(title, excerpt)))
        db.execute("INSERT INTO search(search) VALUES('optimize')")
        db.execute("INSERT INTO short_search(short_search) VALUES('optimize')")


def measure(db_path, workers, rounds, refresh_hz=0):
    app.DB = db_path
    cases = json.loads(CASES.read_text())['cases']
    queries = list(dict.fromkeys([case['query'] for case in cases] + EXTRA_QUERIES))
    with closing(app.connect()) as db:
        pages = db.execute('SELECT count(*) FROM pages').fetchone()[0]
        synthetic = db.execute("SELECT count(*) FROM pages WHERE updated='synthetic'").fetchone()[0]
        digest = hashlib.sha256()
        for row in db.execute('SELECT url,title,body FROM pages ORDER BY url'):
            digest.update((json.dumps(tuple(row), ensure_ascii=False) + '\n').encode())
        journal_mode = db.execute('PRAGMA journal_mode').fetchone()[0]
        refresh_pages = db.execute('SELECT url,title,body FROM pages ORDER BY url LIMIT 64').fetchall() if refresh_hz else []
    refresh_times = []
    stop_refreshing = threading.Event()

    def refresh():
        index = 0
        while not stop_refreshing.is_set():
            started = time.perf_counter()
            app.save_page(*refresh_pages[index % len(refresh_pages)])
            refresh_times.append((time.perf_counter() - started) * 1000)
            index += 1
            stop_refreshing.wait(max(0, 1 / refresh_hz - (time.perf_counter() - started)))

    with app.ThreadingHTTPServer(('127.0.0.1', 0), QuietHandler) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()

        def request(query):
            started = time.perf_counter()
            with closing(http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=30)) as client:
                client.request('GET', '/api/search?' + urlencode({'q': query}))
                response = client.getresponse()
                payload = response.read()
                elapsed = (time.perf_counter() - started) * 1000
                if response.status != 200:
                    raise RuntimeError(f'Unexpected HTTP {response.status} for {query!r}')
                return elapsed, hashlib.sha256(payload).hexdigest()

        try:
            signatures = {query: request(query)[1] for query in queries}
            workload = queries * rounds
            random.Random(42).shuffle(workload)
            with ThreadPoolExecutor(max_workers=workers) as readers, ThreadPoolExecutor(max_workers=1) as writer:
                started = time.perf_counter()
                pending_refresh = writer.submit(refresh) if refresh_hz else None
                try:
                    samples = list(readers.map(request, workload))
                    seconds = time.perf_counter() - started
                finally:
                    stop_refreshing.set()
                if pending_refresh:
                    pending_refresh.result()
        finally:
            server.shutdown()
            thread.join()
    timings = {query: [] for query in queries}
    for query, (elapsed, signature) in zip(workload, samples):
        if signature != signatures[query]:
            raise RuntimeError(f'Inconsistent API response for {query!r}')
        timings[query].append(elapsed)
    values = [elapsed for elapsed, _ in samples]
    return {'environment': {'python': platform.python_version(), 'sqlite': sqlite3.sqlite_version},
            'pages': pages, 'synthetic_pages': synthetic, 'corpus_sha256': digest.hexdigest(),
            'journal_mode': journal_mode, 'refresh_target_per_second': refresh_hz,
            'refreshes': len(refresh_times),
            'refresh_p95_ms': round(percentile(refresh_times, 95), 3) if refresh_times else None,
            'workers': workers, 'rounds': rounds, 'requests': len(values),
            'requests_per_second': round(len(values) / seconds, 1),
            **{f'p{p}_ms': round(percentile(values, p), 3) for p in (50, 95, 99)},
            'queries': {query: {'p95_ms': round(percentile(samples, 95), 3),
                                'response_sha256': signatures[query]} for query, samples in timings.items()}}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db', type=Path, default=app.DB, help='Source index or previously built benchmark DB')
    parser.add_argument('--build', type=Path, help='Build and keep a new benchmark DB at this path')
    parser.add_argument('--pages', type=int, default=10_000)
    parser.add_argument('--workers', type=int, default=8)
    parser.add_argument('--rounds', type=int, default=16)
    parser.add_argument('--refresh-hz', type=int, default=0, help='Maximum same-content page refreshes per second, on a disposable copy')
    parser.add_argument('--journal', choices=('delete', 'wal'), default='wal', help='Journal mode of the disposable refresh workload')
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if not args.db.is_file() or min(args.pages, args.workers, args.rounds) < 1:
        parser.error('An existing DB and positive page, worker and round counts are required')
    if args.build:
        with args.build.open('xb'):
            pass
        build_corpus(args.db.resolve(), args.build, args.pages)
    if args.refresh_hz < 0:
        parser.error('--refresh-hz must be non-negative')
    source = (args.build or args.db).resolve()
    if args.refresh_hz:
        with tempfile.TemporaryDirectory(prefix='little-search-load-') as directory:
            copy = Path(directory) / 'index.db'
            copy_index(source, copy)
            app.DB = copy
            app.init_db()
            with closing(sqlite3.connect(copy)) as db:
                db.execute('PRAGMA journal_mode=' + args.journal)
            report = measure(copy, args.workers, args.rounds, args.refresh_hz)
    else:
        report = measure(source, args.workers, args.rounds)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    print({k: v for k, v in report.items() if k != 'queries'})


if __name__ == '__main__':
    main()
