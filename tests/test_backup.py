import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import app
import backup
import collect


class BackupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.original_db = app.DB
        app.DB = self.root / 'source.db'
        app.init_db()
        app.save_page('https://example.com/', '日本語 Python', '検索本文 Python tutorial')
        app.record_failure('https://example.com/failure', 'HTTP 503')
        collect.init_frontier()
        with app.connect() as db:
            db.execute("INSERT INTO crawls(seed,started,status,indexed,message,events) VALUES ('https://example.com/',?,'completed',1,'OK','[]')", (app.utc_now(),))
            db.execute("INSERT INTO frontier VALUES ('git','https://example.com/next','pending','')")

    def tearDown(self):
        app.DB = self.original_db
        self.temp.cleanup()

    def test_round_trip_preserves_search_history_failures_and_frontier(self):
        snapshot = self.root / 'snapshot.db'
        report = backup.copy_database(app.DB, snapshot)
        self.assertEqual(report['counts'], {'pages': 1, 'search': 1, 'crawls': 1, 'failures': 1, 'frontier': 1})
        self.assertEqual(len(report['sha256']), 64)
        restored = self.root / 'restored.db'
        backup.copy_database(snapshot, restored)
        app.DB = restored
        self.assertEqual(len(app.search('Python')), 1)
        self.assertEqual(len(app.search('日本語')), 1)
        self.assertEqual(len(app.crawl_history()), 1)
        self.assertEqual(len(app.failed_pages()), 1)
        with app.connect() as db:
            self.assertEqual(db.execute('SELECT status FROM frontier').fetchone()[0], 'pending')

    def test_existing_destination_requires_replace(self):
        target = self.root / 'target.db'
        target.write_bytes(b'keep me')
        with self.assertRaises(FileExistsError):
            backup.copy_database(app.DB, target)
        self.assertEqual(target.read_bytes(), b'keep me')
        backup.copy_database(app.DB, target, replace=True)
        with sqlite3.connect(target) as db:
            self.assertEqual(db.execute('SELECT count(*) FROM pages').fetchone()[0], 1)

    def test_corrupt_source_leaves_existing_target_untouched(self):
        corrupt = self.root / 'broken.db'
        corrupt.write_bytes(b'not a database')
        target = self.root / 'keep.db'
        target.write_bytes(b'original contents')
        with self.assertRaises(sqlite3.Error):
            backup.copy_database(corrupt, target, replace=True)
        self.assertEqual(target.read_bytes(), b'original contents')
        self.assertEqual(list(self.root.glob('.little-search-*')), [])

    def test_unrelated_or_mismatched_database_is_rejected(self):
        unrelated = self.root / 'other.db'
        with sqlite3.connect(unrelated) as db:
            db.execute('CREATE TABLE notes (body TEXT)')
        with self.assertRaises(ValueError):
            backup.copy_database(unrelated, self.root / 'out.db')
        with app.connect() as db:
            db.execute('DELETE FROM search')
        with self.assertRaisesRegex(ValueError, '件数'):
            backup.copy_database(app.DB, self.root / 'out.db')
        self.assertFalse((self.root / 'out.db').exists())

    def test_same_file_and_missing_source_are_rejected(self):
        with self.assertRaises(ValueError):
            backup.copy_database(app.DB, app.DB, replace=True)
        with self.assertRaises(ValueError):
            backup.copy_database(self.root / 'missing.db', self.root / 'out.db')
        self.assertFalse((self.root / 'missing.db').exists())

    def test_wal_commits_are_included(self):
        writer = sqlite3.connect(app.DB)
        try:
            writer.execute('PRAGMA journal_mode=WAL')
            app.save_page('https://example.com/wal', 'Committed WAL', 'wal data')
            self.assertTrue(Path(str(app.DB) + '-wal').exists())
            report = backup.copy_database(app.DB, self.root / 'wal-backup.db')
            self.assertEqual(report['counts']['pages'], 2)
        finally:
            writer.close()

    def test_destination_journal_is_not_removed(self):
        target = self.root / 'target.db'
        journal = Path(str(target) + '-wal')
        journal.write_bytes(b'keep journal')
        with self.assertRaisesRegex(ValueError, 'ジャーナル'):
            backup.copy_database(app.DB, target, replace=True)
        self.assertEqual(journal.read_bytes(), b'keep journal')
        self.assertFalse(target.exists())

    def test_cli_create_and_restore(self):
        import io
        import json
        snapshot, restored = self.root / 'cli.db', self.root / 'restored-cli.db'
        with patch('sys.argv', ['backup.py', 'create', str(snapshot), '--db', str(app.DB)]), patch('sys.stdout', new_callable=io.StringIO) as output:
            backup.main()
            self.assertEqual(json.loads(output.getvalue())['counts']['pages'], 1)
        with patch('sys.argv', ['backup.py', 'restore', str(snapshot), '--db', str(restored)]), patch('sys.stdout', new_callable=io.StringIO):
            backup.main()
        self.assertTrue(restored.is_file())
        with patch('sys.argv', ['backup.py', 'restore', str(snapshot), '--db', str(restored)]), patch('sys.stderr', new_callable=io.StringIO), self.assertRaises(SystemExit) as error:
            backup.main()
        self.assertEqual(error.exception.code, 1)

    def test_publication_race_does_not_overwrite_new_file(self):
        target = self.root / 'raced.db'
        def publish(source, destination):
            destination.write_bytes(b'created by another process')
            raise FileExistsError('race')
        with patch.object(backup.os, 'link', side_effect=publish):
            with self.assertRaises(FileExistsError):
                backup.copy_database(app.DB, target)
        self.assertEqual(target.read_bytes(), b'created by another process')
        self.assertEqual(list(self.root.glob('.little-search-*')), [])
