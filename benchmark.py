"""Measure warm local search latency; this is not an HTTP or concurrent-load test."""
import argparse
import hashlib
import json
import math
import platform
import sqlite3
import statistics
import time
from pathlib import Path

import app
from evaluate import CASES

EXTRA_QUERIES = ['Python 3', 'git a', 'SQLite 日本', '京都', '日', 'by', 'nonexistent-query-xyz']


def percentile(values, percent):
    return sorted(values)[max(0, math.ceil(len(values) * percent / 100) - 1)]


def benchmark(rounds=15):
    cases = json.loads(CASES.read_text())['cases']
    queries = list(dict.fromkeys([case['query'] for case in cases] + EXTRA_QUERIES))
    timings = {query: [] for query in queries}
    signatures = {}
    for query in queries:
        app.search_page(query)  # One warm-up per query; no result cache is required.
    for _ in range(rounds):
        for query in queries:
            started = time.perf_counter()
            result = app.search_page(query)
            timings[query].append((time.perf_counter() - started) * 1000)
            signatures[query] = {'total': result['total'], 'top_urls': [row['url'] for row in result['results']]}
    all_values = [value for values in timings.values() for value in values]
    with app.connect() as db:
        count = db.execute('SELECT count(*) FROM pages').fetchone()[0]
    hits = sum(any(url in case['expected_urls'] for url in signatures[case['query']]['top_urls'][:5]) for case in cases)
    content = hashlib.sha256()
    with app.connect() as db:
        for row in db.execute('SELECT url,title,body FROM pages ORDER BY url'):
            content.update(json.dumps(tuple(row), ensure_ascii=False, separators=(',', ':')).encode('utf-8'))
            content.update(b'\n')
    with app.DB.open('rb') as stream:
        db_digest = hashlib.file_digest(stream, 'sha256').hexdigest()
    digest = content.hexdigest()
    return {'measured_at': app.utc_now(), 'environment': {'python': platform.python_version(), 'sqlite': sqlite3.sqlite_version},
            'corpus_sha256': digest, 'db_sha256': db_digest, 'db_bytes': app.DB.stat().st_size, 'pages': count, 'rounds': rounds, 'queries': len(queries), 'requests': len(all_values),
            'p50_ms': round(statistics.median(all_values), 3), 'p95_ms': round(percentile(all_values, 95), 3),
            'p99_ms': round(percentile(all_values, 99), 3), 'max_ms': round(max(all_values), 3), 'hits_at_5': hits,
            'scope': 'warm sequential local search, fresh SQLite connection per request; excludes HTTP and rendering',
            'results': [{'query': query, 'p50_ms': round(statistics.median(values), 3), 'p95_ms': round(percentile(values, 95), 3), **signatures[query]} for query, values in timings.items()]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db', type=Path, default=app.DB)
    parser.add_argument('--rounds', type=int, default=15)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if not args.db.is_file() or not 1 <= args.rounds <= 100:
        parser.error('Existing DB and 1–100 rounds are required')
    app.DB = args.db.resolve()
    report = benchmark(args.rounds)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps({key: value for key, value in report.items() if key != 'results'}, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
