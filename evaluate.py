"""Evaluate the actual local index against a fixed search task set."""
import argparse
import json
import statistics
import time
from pathlib import Path
from urllib.parse import urlsplit

import app

CASES = Path(__file__).with_name('evaluation') / 'cases.json'


def evaluate(cases):
    with app.connect() as db:
        urls = {row[0] for row in db.execute('SELECT url FROM pages')}
    results = []
    for case in cases:
        timings = []
        for _ in range(3):
            started = time.perf_counter()
            found = app.search_page(case['query'], page_size=5)['results']
            timings.append((time.perf_counter() - started) * 1000)
        expected = set(case['expected_urls'])
        rank = next((i for i, row in enumerate(found, 1) if row['url'] in expected), None)
        results.append({'id': case['id'], 'query': case['query'], 'covered': bool(expected & urls), 'rank': rank, 'latency_ms': round(statistics.median(timings), 3)})
    hits = sum(row['rank'] is not None for row in results)
    covered = sum(row['covered'] for row in results)
    worst = max((row['latency_ms'] for row in results), default=0)
    site_counts = app.list_sites()
    expected_hosts = {urlsplit(url).hostname for case in cases for url in case['expected_urls']}
    counts = {site['host']: site['count'] for site in site_counts}
    corpus_pass = len(expected_hosts) == 5 and all(counts.get(host, 0) >= 100 for host in expected_hosts)
    return {'sites': site_counts, 'corpus_pass': corpus_pass, 'pages': len(urls), 'cases': len(results), 'covered': covered, 'hits_at_5': hits,
            'hit_rate': hits / len(results) if results else 0,
            'max_median_latency_ms': worst,
            'quality_pass': len(results) == 20 and hits >= 16,
            'scale_speed_pass': len(urls) >= 500 and worst < 1000,
            'results': results}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db', type=Path, default=app.DB)
    parser.add_argument('--collect', action='store_true', help='Fetch the 20 reference pages, respecting robots.txt')
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    app.DB = args.db.resolve()
    suite = json.loads(CASES.read_text())
    if args.collect:
        app.init_db()
        for case in suite['cases']:
            for url in case['expected_urls']:
                app.cancel_event.clear()
                app.crawl_lock.acquire()
                app.crawl(url, 1)
                print(f"{case['id']}: {app.state['message']}", flush=True)
    if not app.DB.is_file():
        parser.error('DBがありません。先にページを登録するか --collect を指定してください')
    report = evaluate(suite['cases'])
    encoded = json.dumps(report, ensure_ascii=False, indent=2) + '\n'
    if args.output:
        args.output.write_text(encoded)
    print(encoded)


if __name__ == '__main__':
    main()
