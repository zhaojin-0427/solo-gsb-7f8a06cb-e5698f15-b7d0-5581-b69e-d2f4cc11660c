"""SQLite storage layer.

One shared writer connection serialized by a lock (short ``BEGIN IMMEDIATE``
transactions); readers open their own connections and rely on WAL snapshot
isolation, so a single read transaction always sees a consistent view of
documents + postings of one index generation.
"""
from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager

SCHEMA = """
CREATE TABLE IF NOT EXISTS documents (
    doc_id  TEXT PRIMARY KEY,
    text    TEXT NOT NULL,
    version INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS change_log (
    seq     INTEGER PRIMARY KEY AUTOINCREMENT,
    doc_id  TEXT NOT NULL,
    op      TEXT NOT NULL CHECK (op IN ('upsert','delete')),
    version INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS postings (
    gen       INTEGER NOT NULL,
    term      TEXT NOT NULL,
    doc_id    TEXT NOT NULL,
    positions TEXT NOT NULL,  -- JSON array of token positions
    PRIMARY KEY (gen, term, doc_id)
);
CREATE TABLE IF NOT EXISTS indexes (
    gen         INTEGER PRIMARY KEY,
    status      TEXT NOT NULL,  -- building | active | retired | dropped | cancelled
    config_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS rebuild_state (
    id           INTEGER PRIMARY KEY CHECK (id = 1),
    state        TEXT NOT NULL,            -- IDLE | BACKFILL | CATCHUP | SWITCH
    gen          INTEGER,                  -- candidate generation
    owner        TEXT,                     -- fencing token of the rebuild worker
    snapshot_seq INTEGER NOT NULL DEFAULT 0,  -- high-water mark at claim time
    applied_seq  INTEGER NOT NULL DEFAULT 0,  -- catch-up watermark
    pause_points TEXT NOT NULL DEFAULT '[]',  -- drill: boundaries to pause at
    paused_at    TEXT                      -- boundary the worker is parked at
);
"""


class Store:
    def __init__(self, path: str):
        self.path = path
        self._lock = threading.RLock()
        self._w = self._connect()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=10000")
        return conn

    def init_schema(self, default_config_json: str) -> None:
        with self._lock:
            self._w.executescript(SCHEMA)
        with self.write_txn() as c:
            c.execute("INSERT OR IGNORE INTO meta(key, value) VALUES ('active_gen', '1')")
            c.execute("INSERT OR IGNORE INTO meta(key, value) VALUES ('version_seq', '0')")
            c.execute("INSERT OR IGNORE INTO meta(key, value) VALUES ('armed_pause_points', '[]')")
            c.execute(
                "INSERT OR IGNORE INTO indexes(gen, status, config_json) VALUES (1, 'active', ?)",
                (default_config_json,),
            )
            c.execute(
                "INSERT OR IGNORE INTO rebuild_state(id, state) VALUES (1, 'IDLE')"
            )

    @contextmanager
    def write_txn(self):
        with self._lock:
            c = self._w
            c.execute("BEGIN IMMEDIATE")
            try:
                yield c
                c.execute("COMMIT")
            except BaseException:
                c.execute("ROLLBACK")
                raise

    @contextmanager
    def read_txn(self):
        c = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA busy_timeout=10000")
        c.execute("BEGIN")
        try:
            yield c
            c.execute("COMMIT")
        except BaseException:
            c.execute("ROLLBACK")
            raise
        finally:
            c.close()

    def close(self) -> None:
        with self._lock:
            self._w.close()
