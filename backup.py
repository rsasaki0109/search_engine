"""Create and restore validated Little Search SQLite snapshots."""
import argparse
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import tempfile

import app


def validate(db):
    checks = [row[0] for row in db.execute('PRAGMA integrity_check')]
    if checks != ['ok']:
        raise ValueError('SQLiteの整合性検査に失敗しました: ' + '; '.join(checks))
    tables = dict(db.execute("SELECT name,sql FROM sqlite_master WHERE type='table'"))
    if not {'pages', 'search'} <= tables.keys() or 'using fts5' not in (tables['search'] or '').lower():
        raise ValueError('Little Searchのページ・FTS5索引がありません')
    required = {'pages': {'url', 'title', 'body', 'updated'}, 'search': {'url', 'title', 'body'}}
    for table, columns in required.items():
        if not columns <= {row[1] for row in db.execute(f'PRAGMA table_info({table})')}:
            raise ValueError(f'{table}の形式が対応していません')
    # This command checks the FTS index in the temporary snapshot, never the source.
    db.execute("INSERT INTO search(search) VALUES('integrity-check')")
    if 'short_search' in tables:
        db.execute("INSERT INTO short_search(short_search) VALUES('integrity-check')")
    counts = {table: db.execute(f'SELECT count(*) FROM {table}').fetchone()[0]
              for table in ['pages', 'search', 'crawls', 'failures', 'frontier'] if table in tables}
    if counts['pages'] != counts['search']:
        raise ValueError('ページ数と検索索引の件数が一致しません')
    if 'short_search' in tables and db.execute('SELECT count(*) FROM short_search').fetchone()[0] != counts['search']:
        raise ValueError('短い語用の索引と検索索引の件数が一致しません')
    return counts


def copy_database(source, destination, replace=False):
    source, destination = Path(source).resolve(), Path(destination).resolve()
    if not source.is_file():
        raise ValueError(f'元DBがありません: {source}')
    if source == destination or (destination.exists() and os.path.samefile(source, destination)):
        raise ValueError('元DBと保存先は別のファイルにしてください')
    if destination.exists() and not replace:
        raise FileExistsError(f'保存先が既に存在します: {destination}')
    # Replacing a DB with a live/recovery journal could lose committed data.
    for suffix in ('-wal', '-shm', '-journal'):
        if Path(str(destination) + suffix).exists():
            raise ValueError('保存先にジャーナルがあります。アプリを停止し、既存DBの状態を確認してください')
    destination.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(prefix='.little-search-', suffix='.db', dir=destination.parent)
    os.close(handle)
    temporary = Path(temporary)
    try:
        with closing(sqlite3.connect(source.as_uri() + '?mode=ro', uri=True, timeout=10)) as original:
            with closing(sqlite3.connect(temporary)) as snapshot:
                original.backup(snapshot)
                with snapshot:
                    counts = validate(snapshot)
        with temporary.open('rb') as stream:
            digest = hashlib.file_digest(stream, 'sha256').hexdigest()
            os.fsync(stream.fileno())
        if replace:
            os.replace(temporary, destination)
        else:
            # Publish without overwriting a file created while the backup ran.
            os.link(temporary, destination)
        return {'path': str(destination), 'bytes': destination.stat().st_size, 'sha256': digest, 'counts': counts}
    finally:
        temporary.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    create = commands.add_parser('create', help='Create a consistent snapshot using the SQLite backup API')
    create.add_argument('destination', type=Path, nargs='?', default=Path(__file__).with_name('backups') / ('search-' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ') + '.db'))
    create.add_argument('--db', type=Path, default=app.DB)
    restore = commands.add_parser('restore', help='Restore a snapshot; stop the app and collectors first')
    restore.add_argument('source', type=Path)
    restore.add_argument('--db', type=Path, default=app.DB)
    restore.add_argument('--replace', action='store_true', help='Replace an existing DB after validation; stop the app first')
    args = parser.parse_args()
    try:
        if args.command == 'create':
            report = copy_database(args.db, args.destination)
        else:
            report = copy_database(args.source, args.db, args.replace)
    except (OSError, sqlite3.Error, ValueError) as exc:
        parser.exit(1, f'失敗: {exc}\n')
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
