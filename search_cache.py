"""Bounded search responses that follow commits from other SQLite connections."""
from collections import OrderedDict
import sqlite3
import threading


class SearchResponseCache:
    def __init__(self, path, max_entries=256):
        self.path = path
        self.max_entries = max_entries
        self.max_bytes = 2 * 1024 * 1024
        self.lock = threading.Lock()
        self.entries = OrderedDict()
        self.bytes = 0
        self.observer = None
        self.identity = None
        self.version = None
        self.generation = 0
        self.closed = False

    def _clear(self):
        self.entries.clear()
        self.bytes = 0
        self.generation += 1

    def _disconnect(self):
        if self.observer is not None:
            self.observer.close()
            self.observer = None
        self.identity = self.version = None
        self._clear()

    def _observe(self):
        """Read data_version on the same connection, with no open transaction."""
        if self.closed:
            return None
        try:
            stat = self.path.stat()
            identity = (stat.st_dev, stat.st_ino)
            if identity != self.identity:
                self._disconnect()
                self.observer = sqlite3.connect(
                    self.path.resolve().as_uri() + '?mode=ro',
                    uri=True, timeout=0.05, check_same_thread=False,
                )
                self.identity = identity
            version = self.observer.execute('PRAGMA data_version').fetchone()[0]
        except (OSError, sqlite3.Error):
            self._disconnect()
            return None
        if version != self.version:
            self._clear()
            self.version = version
        return self.generation

    def response(self, key, compute):
        if not self.max_entries:
            return compute()
        with self.lock:
            generation = self._observe()
            if key in self.entries:
                self.entries.move_to_end(key)
                return self.entries[key]

        data = compute()
        if generation is None or len(data) > self.max_bytes:
            return data
        with self.lock:
            # A search started before a commit must not refill the new generation.
            if self._observe() != generation:
                return data
            old = self.entries.pop(key, b'')
            self.entries[key] = data
            self.bytes += len(data) - len(old)
            while len(self.entries) > self.max_entries or self.bytes > self.max_bytes:
                _, evicted = self.entries.popitem(last=False)
                self.bytes -= len(evicted)
        return data

    def close(self):
        with self.lock:
            self.closed = True
            self._disconnect()
